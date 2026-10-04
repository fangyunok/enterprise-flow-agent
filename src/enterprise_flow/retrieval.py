"""Policy retrieval pipeline: scoped filtering, vector recall and cross-encoder reranking.

The pipeline is deliberately ordered so that authorization is decided **before** retrieval and never
by it:

1. the scoped SQL query produces the only candidate set a caller may read (tenant, department,
   effective window);
2. vector recall ranks inside that set;
3. a cross-encoder reranks the fused candidates;
4. every stage annotates where a row came from, so a reviewer can audit the decision.

Every backend is optional. Without a model service the pipeline degrades to the deterministic
keyword path, which keeps fixture mode and CI reproducible on any machine.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any, Protocol, Sequence

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"
DEFAULT_TOP_K = 20
DEFAULT_TOP_N = 10
RRF_K = 60


class RetrievalError(RuntimeError):
    """Raised when a configured retrieval backend cannot serve a request."""


def document_text(row: dict[str, Any]) -> str:
    """Flatten a policy row into the text that is embedded and reranked."""
    parts = [row.get("title"), row.get("kind"), row.get("city"), row.get("content")]
    return " ".join(str(part) for part in parts if part)


def keyword_terms(text: str) -> list[str]:
    """Character and word terms used by the deterministic keyword stage."""
    return re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", str(text).casefold())


def keyword_score(query: str, row: dict[str, Any]) -> int:
    terms = set(keyword_terms(query))
    if not terms:
        return 0
    text = (str(row.get("title", "")) + str(row.get("content", "")) + str(row.get("kind", ""))).casefold()
    return sum(term in text for term in terms)


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion over several ranked id lists."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank + 1)
    return fused


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class HashEmbeddingProvider:
    """Deterministic offline embedder built from hashed character n-grams.

    It is a real vector space (L2-normalised, cosine comparable), not a stub: fixture mode, CI and
    every machine without a model service use it to exercise the full recall path.
    """

    name = "hash-ngram"

    def __init__(self, dimension: int = 512):
        self.dimension = dimension

    @staticmethod
    def _tokens(text: str) -> list[str]:
        folded = str(text).casefold()
        words = re.findall(r"[a-z0-9]+", folded)
        chars = re.findall(r"[\u4e00-\u9fff]", folded)
        bigrams = ["".join(chars[index:index + 2]) for index in range(max(len(chars) - 1, 0))]
        return words + chars + bigrams

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in self._tokens(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                sign = 1.0 if digest[4] % 2 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class HttpEmbeddingProvider:
    """OpenAI-compatible ``/embeddings`` client (vLLM, TEI, Ollama, hosted providers)."""

    def __init__(self, base_url: str, model: str = DEFAULT_EMBEDDING_MODEL, api_key: str | None = None,
                 dimension: int = 1024, timeout: float = 30.0, transport=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dimension = dimension
        self.timeout = timeout
        self.transport = transport
        self.name = "http:" + model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        payload = {"model": self.model, "input": list(texts)}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False, follow_redirects=False) as client:
                response = client.post(self.base_url + "/embeddings", json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # network, TLS, provider envelope
            raise RetrievalError(f"embedding service unavailable: {type(exc).__name__}") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RetrievalError("embedding service returned an unexpected envelope")
        vectors = []
        for item in sorted(data, key=lambda entry: entry.get("index", 0)):
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise RetrievalError("embedding vector missing")
            vectors.append([float(value) for value in vector])
        self.dimension = len(vectors[0])
        return vectors


class SentenceTransformerEmbeddingProvider:
    """Local dense encoder (BGE-M3 by default) loaded through sentence-transformers."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL, device: str | None = None, batch_size: int = 32):
        self.model_name = model_name
        self.batch_size = batch_size
        self.device = device
        self._model = None
        self.name = "local:" + model_name

    @property
    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RetrievalError("sentence-transformers is not installed; install the retrieval extra") from exc
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    @property
    def dimension(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.model.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return [[float(value) for value in vector] for vector in vectors]


class VectorStore(Protocol):
    name: str
    dimension: int

    def upsert(self, records: Sequence[dict[str, Any]]) -> int:
        ...

    def search(self, vector: Sequence[float], *, top_k: int, allowed_ids: Sequence[str] | None = None) -> list[tuple[str, float]]:
        ...


class LocalVectorStore:
    """Exact in-process index. Uses FAISS flat inner product when available, numpy otherwise."""

    def __init__(self, dimension: int):
        self.dimension = dimension
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._faiss = None
        self._index = None
        try:
            import faiss
            self._faiss = faiss
            self._index = faiss.IndexFlatIP(dimension)
            self.name = "local:faiss-flat-ip"
        except ImportError:
            self.name = "local:numpy-cosine"

    def __len__(self) -> int:
        return len(self._ids)

    def upsert(self, records: Sequence[dict[str, Any]]) -> int:
        if not records:
            return 0
        incoming = {record["id"]: [float(value) for value in record["vector"]] for record in records}
        if any(len(vector) != self.dimension for vector in incoming.values()):
            raise RetrievalError("vector dimension mismatch")
        kept = [(identifier, vector) for identifier, vector in zip(self._ids, self._vectors) if identifier not in incoming]
        self._ids = [identifier for identifier, _ in kept]
        self._vectors = [vector for _, vector in kept]
        self._ids.extend(incoming)
        self._vectors.extend(incoming.values())
        if self._faiss is not None:
            self._index = self._faiss.IndexFlatIP(self.dimension)
            if self._vectors:
                import numpy

                self._index.add(numpy.asarray(self._vectors, dtype="float32"))
        return len(incoming)

    def search(self, vector: Sequence[float], *, top_k: int, allowed_ids: Sequence[str] | None = None) -> list[tuple[str, float]]:
        if not self._vectors:
            return []
        allowed = None if allowed_ids is None else set(allowed_ids)
        if self._faiss is not None and allowed is None:
            import numpy

            query = numpy.asarray([list(vector)], dtype="float32")
            scores, indices = self._index.search(query, min(top_k, len(self._ids)))
            return [(self._ids[index], float(score)) for score, index in zip(scores[0], indices[0]) if index >= 0]
        scored = []
        for identifier, stored in zip(self._ids, self._vectors):
            if allowed is not None and identifier not in allowed:
                continue
            scored.append((identifier, sum(left * right for left, right in zip(stored, vector))))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]


class MilvusVectorStore:
    """Milvus collection with scalar scope fields, so filtering happens inside the index."""

    def __init__(self, uri: str = "http://127.0.0.1:19530", collection: str = "enterprise_policy",
                 dimension: int = 1024, token: str | None = None, metric: str = "COSINE"):
        self.uri = uri
        self.collection = collection
        self.dimension = dimension
        self.token = token
        self.metric = metric
        self.name = "milvus:" + collection
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise RetrievalError("pymilvus is not installed; install the milvus extra") from exc
            self._client = MilvusClient(uri=self.uri, token=self.token or "") if self.token else MilvusClient(uri=self.uri)
            if not self._client.has_collection(self.collection):
                self._client.create_collection(collection_name=self.collection, dimension=self.dimension,
                                               metric_type=self.metric, auto_id=False,
                                               primary_field_name="id", id_type="string", max_length=128,
                                               vector_field_name="vector", enable_dynamic_field=True)
        return self._client

    def upsert(self, records: Sequence[dict[str, Any]]) -> int:
        if not records:
            return 0
        rows = []
        for record in records:
            scope = record.get("scope") or {}
            rows.append({"id": record["id"], "vector": [float(value) for value in record["vector"]],
                         "tenant_id": scope.get("tenant_id"), "department_scope": scope.get("department_scope"),
                         "city": scope.get("city"), "kind": scope.get("kind"), "version": scope.get("version"),
                         "effective_from": scope.get("effective_from"), "effective_to": scope.get("effective_to")})
        self.client.upsert(collection_name=self.collection, data=rows)
        return len(rows)

    @staticmethod
    def scope_expression(scope: dict[str, Any] | None) -> str:
        """Push tenant / department / effective-date filtering into Milvus itself."""
        if not scope:
            return ""
        clauses = []
        if scope.get("tenant_id"):
            clauses.append(f'tenant_id == "{scope["tenant_id"]}"')
        if scope.get("department_id"):
            clauses.append(f'(department_scope == "{scope["department_id"]}" or department_scope == "*")')
        if scope.get("business_date"):
            day = scope["business_date"]
            clauses.append(f'effective_from <= "{day}"')
            clauses.append(f'(effective_to == "" or effective_to > "{day}")')
        return " and ".join(clauses)

    def search(self, vector: Sequence[float], *, top_k: int, allowed_ids: Sequence[str] | None = None) -> list[tuple[str, float]]:
        expression = ""
        if allowed_ids is not None:
            quoted = ", ".join('"' + str(identifier).replace('"', "") + '"' for identifier in allowed_ids)
            expression = "id in [" + quoted + "]"
        hits = self.client.search(collection_name=self.collection, data=[[float(value) for value in vector]],
                                  limit=top_k, output_fields=["id"], filter=expression)
        results: list[tuple[str, float]] = []
        for hit in hits[0] if hits else []:
            identifier = hit.get("id") or (hit.get("entity") or {}).get("id")
            if identifier is not None:
                results.append((str(identifier), float(hit.get("distance", 0.0))))
        return results


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        ...


class NoOpReranker:
    """Keeps the fused order; used when no rerank service is configured."""

    name = "none"

    def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        return [(index, 0.0) for index in range(min(top_n, len(texts)))]


class CrossEncoderReranker:
    """Local cross-encoder reranker (bge-reranker family) through sentence-transformers."""

    def __init__(self, model_name: str = DEFAULT_RERANKER_MODEL, device: str | None = None, max_length: int = 512):
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self.name = "local:" + model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RetrievalError("sentence-transformers is not installed; install the retrieval extra") from exc
            self._model = CrossEncoder(self.model_name, device=self.device, max_length=self.max_length)
        return self._model

    def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        if not texts:
            return []
        scores = self.model.predict([(query, text) for text in texts])
        ranked = sorted(((index, float(score)) for index, score in enumerate(scores)), key=lambda item: (-item[1], item[0]))
        return ranked[:top_n]


class HttpReranker:
    """Text-Embeddings-Inference compatible ``/rerank`` client."""

    def __init__(self, base_url: str, model: str = DEFAULT_RERANKER_MODEL, api_key: str | None = None,
                 timeout: float = 30.0, transport=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport
        self.name = "http:" + model

    def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        import httpx

        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        payload = {"model": self.model, "query": query, "texts": list(texts), "top_n": top_n, "raw_scores": True}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
                response = client.post(self.base_url + "/rerank", json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise RetrievalError(f"rerank service unavailable: {type(exc).__name__}") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise RetrievalError("rerank service returned an unexpected envelope")
        return [(int(item["index"]), float(item["score"])) for item in results[:top_n]]


class PolicyRetriever:
    """Scoped filter → hybrid recall (keyword ∪ vector, RRF) → optional cross-encoder rerank."""

    def __init__(self, embedder: EmbeddingProvider | None = None, store: VectorStore | None = None,
                 reranker: Reranker | None = None, top_k: int = DEFAULT_TOP_K, top_n: int = DEFAULT_TOP_N):
        self.embedder = embedder
        self.store = store
        self.reranker = reranker
        self.top_k = top_k
        self.top_n = top_n
        self._fingerprint: str | None = None
        self._indexed = 0

    @property
    def vector_enabled(self) -> bool:
        return self.embedder is not None and self.store is not None

    @property
    def rerank_enabled(self) -> bool:
        return self.reranker is not None and not isinstance(self.reranker, NoOpReranker)

    @property
    def mode(self) -> str:
        if not self.vector_enabled:
            return "keyword"
        return "hybrid+rerank" if self.rerank_enabled else "hybrid"

    @property
    def indexed_documents(self) -> int:
        return self._indexed

    @staticmethod
    def fingerprint(rows: Sequence[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for row in sorted(rows, key=lambda item: str(item.get("policy_id"))):
            digest.update("|".join([str(row.get("policy_id")), str(row.get("version")), str(row.get("content"))]).encode("utf-8"))
        return digest.hexdigest()

    def refresh(self, rows: Sequence[dict[str, Any]]) -> int:
        """Embed and index the corpus once per content change; re-indexing is a no-op otherwise."""
        if not self.vector_enabled:
            return 0
        fingerprint = self.fingerprint(rows)
        if fingerprint == self._fingerprint:
            return 0
        records = []
        texts = [document_text(row) for row in rows]
        vectors = self.embedder.embed(texts)
        for row, vector in zip(rows, vectors):
            records.append({"id": str(row["policy_id"]), "vector": vector, "scope": {
                "tenant_id": row.get("tenant_id"), "department_scope": row.get("department_scope"),
                "city": row.get("city"), "kind": row.get("kind"), "version": row.get("version"),
                "effective_from": row.get("effective_from"), "effective_to": row.get("effective_to") or ""}})
        self._indexed = self.store.upsert(records)
        self._fingerprint = fingerprint
        return self._indexed

    def search(self, rows: Sequence[dict[str, Any]], query: str, *, top_k: int | None = None,
               top_n: int | None = None) -> list[dict[str, Any]]:
        """Rank only the rows the caller is already authorized to read."""
        scoped = [dict(row) for row in rows]
        limit = top_n or self.top_n
        if not scoped:
            return []
        for row in scoped:
            row["keyword_score"] = keyword_score(query, row)
        keyword_ranking = [row["policy_id"] for row in sorted(scoped, key=lambda item: (-item["keyword_score"], item["policy_id"])) if row["keyword_score"] > 0]
        if not query.strip() or not self.vector_enabled:
            for row in scoped:
                row["retrieval_score"] = row["keyword_score"]
                row["retrieval_path"] = "keyword"
            ordered = [row for row in sorted(scoped, key=lambda item: (-item["retrieval_score"], item["policy_id"])) if row["retrieval_score"] > 0]
            return ordered[:limit]
        allowed = {row["policy_id"]: row for row in scoped}
        candidates = top_k or self.top_k
        vector = self.embedder.embed([query])[0]
        vector_hits = [(identifier, score) for identifier, score in self.store.search(vector, top_k=candidates, allowed_ids=list(allowed)) if identifier in allowed]
        vector_ranking = [identifier for identifier, _ in vector_hits]
        vector_scores = dict(vector_hits)
        fused = rrf_fuse([keyword_ranking[:candidates], vector_ranking])
        for row in scoped:
            row["vector_score"] = vector_scores.get(row["policy_id"])
            row["rrf_score"] = fused.get(row["policy_id"])
        order = [row["policy_id"] for row in sorted(scoped, key=lambda item: (-(item["rrf_score"] or 0.0), item["policy_id"])) if row["rrf_score"]]
        order = order[:candidates]
        if self.rerank_enabled and order:
            texts = [document_text(allowed[identifier]) for identifier in order]
            reranked = self.reranker.rerank(query, texts, limit)
            names = {index: identifier for index, identifier in enumerate(order)}
            order = [names[index] for index, _ in reranked if index in names]
            for row in scoped:
                row["rerank_score"] = None
            for index, score in reranked:
                if index in names:
                    scoped_by_id = allowed[names[index]]
                    scoped_by_id["rerank_score"] = score
            order = [identifier for identifier in order if identifier in allowed]
        for row in scoped:
            if row["policy_id"] in order:
                row["retrieval_score"] = row["rerank_score"] if row.get("rerank_score") is not None else (row.get("rrf_score") or 0.0)
        results = [scoped_row for scoped_row in (allowed[identifier] for identifier in order)]
        for row in results:
            row["retrieval_path"] = self.mode
        return results[:limit]


def build_retriever(mode: str | None = None, *, env: dict[str, str] | None = None) -> PolicyRetriever | None:
    """Build the retriever from environment configuration.

    ``mode=keyword`` (or no embedding endpoint configured) returns ``None`` so the caller keeps the
    deterministic keyword path.
    """
    config = dict(os.environ if env is None else env)
    mode = (mode or config.get("ENTERPRISE_RETRIEVAL_MODE") or "keyword").casefold()
    if mode == "keyword":
        return None
    embed_base = config.get("ENTERPRISE_EMBED_BASE")
    store_kind = (config.get("ENTERPRISE_VECTOR_STORE") or "local").casefold()
    dimension = int(config.get("ENTERPRISE_EMBED_DIMENSION") or 1024)
    if embed_base:
        embedder: EmbeddingProvider = HttpEmbeddingProvider(embed_base, config.get("ENTERPRISE_EMBED_MODEL", DEFAULT_EMBEDDING_MODEL),
                                                           config.get("ENTERPRISE_EMBED_KEY"), dimension=dimension)
    else:
        try:
            embedder = SentenceTransformerEmbeddingProvider(config.get("ENTERPRISE_EMBED_MODEL", DEFAULT_EMBEDDING_MODEL))
        except Exception:
            return None
    if store_kind == "milvus":
        store: VectorStore = MilvusVectorStore(uri=config.get("ENTERPRISE_MILVUS_URI", "http://127.0.0.1:19530"),
                                               collection=config.get("ENTERPRISE_MILVUS_COLLECTION", "enterprise_policy"),
                                               dimension=dimension, token=config.get("ENTERPRISE_MILVUS_TOKEN") or None)
    else:
        store = LocalVectorStore(dimension)
    reranker: Reranker = NoOpReranker()
    rerank_base = config.get("ENTERPRISE_RERANK_BASE")
    if mode == "hybrid+rerank":
        if rerank_base:
            reranker = HttpReranker(rerank_base, config.get("ENTERPRISE_RERANK_MODEL", DEFAULT_RERANKER_MODEL),
                                    config.get("ENTERPRISE_RERANK_KEY"))
        else:
            reranker = CrossEncoderReranker(config.get("ENTERPRISE_RERANK_MODEL", DEFAULT_RERANKER_MODEL))
    return PolicyRetriever(embedder=embedder, store=store, reranker=reranker,
                           top_k=int(config.get("ENTERPRISE_RETRIEVAL_TOP_K") or DEFAULT_TOP_K),
                           top_n=int(config.get("ENTERPRISE_RETRIEVAL_TOP_N") or DEFAULT_TOP_N))
