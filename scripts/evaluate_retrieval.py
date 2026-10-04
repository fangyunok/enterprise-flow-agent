"""Measure scoped policy retrieval: keyword baseline vs hybrid recall vs hybrid + cross-encoder rerank.

The evaluation loads the generated corpus into a temporary database, runs every labelled query under
three retrieval configurations, and reports Recall@k, MRR and an authorization-leak count. The leak
count is an invariant, not a metric: it must stay zero, because retrieval is not allowed to widen the
scoped SQL candidate set.

Usage::

    python scripts/evaluate_retrieval.py --embedder hash --reranker none --label "hash baseline"
    python scripts/evaluate_retrieval.py --embedder local --reranker local --label "BGE-M3 + bge-reranker-base"
    python scripts/evaluate_retrieval.py --embedder http --reranker http --label "GPU: vLLM + TEI"
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enterprise_flow.database import Database  # noqa: E402
from enterprise_flow.retrieval import (  # noqa: E402
    CrossEncoderReranker, HashEmbeddingProvider, HttpEmbeddingProvider, HttpReranker,
    LocalVectorStore, MilvusVectorStore, NoOpReranker, PolicyRetriever,
    SentenceTransformerEmbeddingProvider,
)
from enterprise_flow.schemas import Principal  # noqa: E402
from enterprise_flow.service import EnterpriseService  # noqa: E402

CORPUS_PATH = ROOT / "data" / "policy_corpus.json"
QUERY_PATH = ROOT / "data" / "retrieval_eval_queries.jsonl"
REPORT_PATH = ROOT / "results" / "retrieval_eval.md"
RAW_PATH = ROOT / "results" / "retrieval_eval.json"
K_VALUES = (1, 3, 5)


def load_queries() -> list[dict]:
    return [json.loads(line) for line in QUERY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def prepare_database(corpus: list[dict], queries: list[dict]) -> EnterpriseService:
    folder = tempfile.TemporaryDirectory(prefix="retrieval-eval-")
    service = EnterpriseService(Path(folder.name) / "eval.sqlite")
    service.seed_demo()
    identities = {(row["tenant_id"], row["department_scope"]) for row in corpus if row["department_scope"] != "*"}
    identities |= {(query["tenant_id"], query["department_id"]) for query in queries}
    with service.database.transaction(write=True) as connection:
        for row in corpus:
            connection.execute(
                "INSERT OR REPLACE INTO policies(policy_id,tenant_id,department_scope,kind,city,version,effective_from,effective_to,clause_id,title,content,cap_cents)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["policy_id"], row["tenant_id"], row["department_scope"], row["kind"], row["city"], row["version"],
                 row["effective_from"], row["effective_to"], row["clause_id"], row["title"], row["content"], row["cap_cents"]),
            )
        for tenant, department in sorted(identities):
            connection.execute("INSERT OR IGNORE INTO users(user_id,tenant_id,department_id,display_name,active) VALUES (?,?,?,?,1)",
                               (f"eval-{tenant}-{department}", tenant, department, f"Eval {department}"))
    service._folder = folder  # keep the temporary directory alive for the process lifetime
    return service


def build_retriever(arguments, dimension: int) -> PolicyRetriever | None:
    if arguments.mode == "keyword":
        return None
    if arguments.embedder == "hash":
        embedder = HashEmbeddingProvider(dimension=dimension)
    elif arguments.embedder == "http":
        embedder = HttpEmbeddingProvider(arguments.embed_base, arguments.embed_model, arguments.embed_key, dimension=dimension)
    else:
        embedder = SentenceTransformerEmbeddingProvider(arguments.embed_model, device=arguments.device)
    dimension = embedder.dimension
    if arguments.vector_store == "milvus":
        store = MilvusVectorStore(uri=arguments.milvus_uri, collection=arguments.milvus_collection,
                                  dimension=dimension, token=arguments.milvus_token or None)
    else:
        store = LocalVectorStore(dimension)
    if arguments.reranker == "none":
        reranker = NoOpReranker()
    elif arguments.reranker == "http":
        reranker = HttpReranker(arguments.rerank_base, arguments.rerank_model, arguments.rerank_key)
    else:
        reranker = CrossEncoderReranker(arguments.rerank_model, device=arguments.device)
    return PolicyRetriever(embedder=embedder, store=store, reranker=reranker, top_k=arguments.top_k, top_n=arguments.top_n)


def run(service: EnterpriseService, queries: list[dict], k_values=K_VALUES) -> dict:
    hits = {k: 0 for k in k_values}
    reciprocal = []
    leaks = 0
    empty = 0
    latencies = []
    per_query = []
    candidate_sizes = []
    for query in queries:
        principal = Principal(user_id=f"eval-{query['tenant_id']}-{query['department_id']}", tenant_id=query["tenant_id"],
                              department_id=query["department_id"], display_name=f"Eval {query['department_id']}")
        with service.database.transaction() as connection:
            allowed = {row["policy_id"] for row in service._policy_rows(connection, principal, query["trip_date"])}
        started = time.perf_counter()
        results = service.search_policies(principal, query["query"], query["trip_date"])
        latencies.append((time.perf_counter() - started) * 1000)
        returned = [row["policy_id"] for row in results]
        leaks += sum(1 for identifier in returned if identifier not in allowed)
        candidate_sizes.append(len(allowed))
        if not results:
            empty += 1
        expected = set(query["expected_policy_ids"])
        rank = next((index + 1 for index, identifier in enumerate(returned) if identifier in expected), None)
        for k in k_values:
            hits[k] += int(rank is not None and rank <= k)
        reciprocal.append(1.0 / rank if rank else 0.0)
        per_query.append({"query_id": query["query_id"], "origin": query["origin"], "query": query["query"],
                          "expected": sorted(expected), "returned": returned[:5], "rank": rank})
    total = len(queries) or 1
    human = [entry for entry in per_query if entry["origin"] == "human"]
    return {
        "queries": len(queries),
        "recall": {f"@{k}": hits[k] / total for k in k_values},
        "mrr": sum(reciprocal) / total,
        "leaks": leaks,
        "empty_results": empty,
        "latency_p50_ms": round(statistics.median(latencies), 2),
        "scoped_pool_p50": statistics.median(candidate_sizes) if candidate_sizes else 0,
        "scoped_pool_max": max(candidate_sizes) if candidate_sizes else 0,
        "per_query": per_query,
        "human_recall_at_1": (sum(1 for entry in human if entry["rank"] == 1) / len(human)) if human else None,
    }


def render(label: str, mode: str, embedder_name: str, reranker_name: str, store_name: str,
           measured: dict, indexed: int, arguments, repeated: bool) -> str:
    lines = [f"### {label}", "",
             f"- 配置：retrieval_mode=`{mode}` | embedder=`{embedder_name}` | reranker=`{reranker_name}` | store=`{store_name}`",
             f"- 语料：{measured.get('corpus_clauses', 0)} 条条款"
             + (f"（向量索引入库 {indexed} 条，同一索引反复复用：{'否' if repeated else '是'}）" if indexed else "（关键词路径不建向量索引）"),
             f"- 查询集：{measured['queries']} 条（含人工撰写 {sum(1 for entry in measured['per_query'] if entry['origin'] == 'human')} 条）", "",
             "| 指标 | 值 |", "| --- | --- |",
             f"| Recall@1 | {measured['recall']['@1']:.3f} |",
             f"| Recall@3 | {measured['recall']['@3']:.3f} |",
             f"| Recall@5 | {measured['recall']['@5']:.3f} |",
             f"| MRR | {measured['mrr']:.3f} |",
             f"| 空结果查询 | {measured['empty_results']} |",
             f"| 越权泄漏（必须为 0） | {measured['leaks']} |",
             f"| 授权候选集 P50 / 最大条款数 | {measured['scoped_pool_p50']} / {measured['scoped_pool_max']} |",
             f"| 单查询 P50 延迟 | {measured['latency_p50_ms']} ms |", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate scoped policy retrieval")
    parser.add_argument("--mode", choices=["keyword", "hybrid", "hybrid+rerank"], default=None,
                        help="retrieval mode; defaults to a value implied by the embedder/reranker flags")
    parser.add_argument("--embedder", choices=["hash", "local", "http"], default="hash")
    parser.add_argument("--embed-model", default="BAAI/bge-m3")
    parser.add_argument("--embed-base", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--embed-key", default=None)
    parser.add_argument("--embed-dimension", type=int, default=1024)
    parser.add_argument("--vector-store", choices=["local", "milvus"], default="local")
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--milvus-collection", default="enterprise_policy_eval")
    parser.add_argument("--milvus-token", default=None)
    parser.add_argument("--reranker", choices=["none", "local", "http"], default="none")
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-base")
    parser.add_argument("--rerank-base", default="http://127.0.0.1:8080")
    parser.add_argument("--rerank-key", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--label", default=None)
    parser.add_argument("--append", action="store_true", help="append the section instead of rewriting the report")
    parser.add_argument("--explain", type=int, default=0, metavar="N", help="print N ranking misses for diagnosis")
    arguments = parser.parse_args()

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["policies"]
    queries = load_queries()
    service = prepare_database(corpus, queries)

    if arguments.mode is None:
        arguments.mode = "keyword" if arguments.embedder == "hash" and arguments.reranker == "none" and arguments.label is None else ("hybrid+rerank" if arguments.reranker != "none" else "hybrid")
    retriever = build_retriever(arguments, arguments.embed_dimension)
    service.retriever = retriever
    indexed = service.refresh_index()

    started = time.perf_counter()
    measured = run(service, queries)
    measured["wall_clock_s"] = round(time.perf_counter() - started, 2)
    measured["mode"] = service.retrieval_mode
    measured["embedder"] = retriever.embedder.name if retriever and retriever.embedder else "keyword-only"
    measured["reranker"] = retriever.reranker.name if retriever and retriever.reranker else "none"
    measured["store"] = retriever.store.name if retriever and retriever.store else "none"
    measured["indexed_documents"] = indexed
    measured["corpus_clauses"] = len(corpus)

    label = arguments.label or measured["mode"]
    section = render(label, measured["mode"], measured["embedder"], measured["reranker"], measured["store"],
                     measured, measured["indexed_documents"], arguments, repeated=arguments.append)

    header = "# 检索评测记录\n\n数据集由 `scripts/build_retrieval_dataset.py` 生成，评测由 `scripts/evaluate_retrieval.py` 运行。\n\
召回口径为「期望条款是否出现在前 k 条」，MRR 取首个命中排名的倒数；越权泄漏统计检索结果中不属于\n\
当前身份授权范围的条款数量，该值必须恒为 0。\n\n"
    if arguments.append and REPORT_PATH.exists():
        REPORT_PATH.write_text(REPORT_PATH.read_text(encoding="utf-8") + "\n" + section, encoding="utf-8")
    else:
        REPORT_PATH.write_text(header + section, encoding="utf-8")

    existing = json.loads(RAW_PATH.read_text(encoding="utf-8")) if (arguments.append and RAW_PATH.exists()) else []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append({key: value for key, value in measured.items() if key != "per_query"}
                    | {"label": label, "unranked": sum(1 for entry in measured["per_query"] if entry["rank"] is None)})

    if arguments.explain:
        misses = [entry for entry in measured["per_query"] if entry["rank"] is None or entry["rank"] > 1]
        print(f"\n### 排序失分样例（前 {min(len(misses), arguments.explain)} 条，共 {len(misses)} 条未排到首位）\n")
        for entry in misses[:arguments.explain]:
            print(f"- Q: {entry['query']}")
            print(f"  期望: {entry['expected'][0]} | 实际排名: {entry['rank']}")
            print(f"  返回前 3: {entry['returned'][:3]}")
    RAW_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(section)
    print(f"report: {REPORT_PATH.relative_to(ROOT)}  raw: {RAW_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
