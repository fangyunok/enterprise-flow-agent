"""Tests for the scoped policy retrieval pipeline.

The properties under test are the ones that keep retrieval safe to enable in a business flow:
authorization is decided by the scoped query and never by ranking, a missing model service falls
back to the deterministic keyword path, and the corpus index is built once per content change.
"""

import os
import tempfile
import unittest
from pathlib import Path

from enterprise_flow.retrieval import (
    CrossEncoderReranker,
    HashEmbeddingProvider,
    LocalVectorStore,
    MilvusVectorStore,
    NoOpReranker,
    PolicyRetriever,
    RetrievalError,
    build_retriever,
    document_text,
    keyword_score,
    rrf_fuse,
)
from enterprise_flow.service import EnterpriseService

ROWS = [
    {"policy_id": "p-1", "tenant_id": "alpha", "department_scope": "engineering", "kind": "hotel",
     "city": "sh", "version": 2, "effective_from": "2026-01-01", "effective_to": None,
     "content": "上海住宿每晚上限 500 元"},
    {"policy_id": "p-2", "tenant_id": "alpha", "department_scope": "*", "kind": "hotel",
     "city": "sh", "version": 1, "effective_from": "2026-01-01", "effective_to": None,
     "content": "住宿费用需凭发票报销"},
    {"policy_id": "p-3", "tenant_id": "beta", "department_scope": "engineering", "kind": "hotel",
     "city": "sh", "version": 1, "effective_from": "2026-01-01", "effective_to": None,
     "content": "异地住宿报销额度为 400 元"},
]


class KeywordScoringTest(unittest.TestCase):
    def test_scores_only_contained_terms(self):
        self.assertGreater(keyword_score("住宿上限", ROWS[0]), 0)
        self.assertEqual(keyword_score("潜水艇", ROWS[0]), 0)

    def test_empty_query_scores_zero(self):
        self.assertEqual(keyword_score("   ", ROWS[0]), 0)

    def test_document_text_joins_scope_and_content(self):
        text = document_text(ROWS[0])
        self.assertIn("上海住宿每晚上限 500 元", text)
        self.assertIn("hotel", text)

    def test_rrf_fuse_rewards_agreement(self):
        fused = rrf_fuse([["a", "b"], ["b", "a"]])
        self.assertAlmostEqual(fused["a"], fused["b"], places=6)
        self.assertGreater(fused["a"], rrf_fuse([["a"], ["b"]])["a"])


class LocalStoreTest(unittest.TestCase):
    def test_upsert_and_search_returns_cosine_ordered_hits(self):
        store = LocalVectorStore(4)
        store.upsert([
            {"id": "a", "vector": [1.0, 0.0, 0.0, 0.0]},
            {"id": "b", "vector": [0.0, 1.0, 0.0, 0.0]},
        ])
        self.assertEqual(store.search([1.0, 0.0, 0.0, 0.0], top_k=2)[0][0], "a")

    def test_allowed_ids_restricts_the_candidate_set(self):
        store = LocalVectorStore(4)
        store.upsert([
            {"id": "a", "vector": [1.0, 0.0, 0.0, 0.0]},
            {"id": "b", "vector": [0.9, 0.1, 0.0, 0.0]},
        ])
        hits = store.search([1.0, 0.0, 0.0, 0.0], top_k=5, allowed_ids=["b"])
        self.assertEqual([identifier for identifier, _ in hits], ["b"])

    def test_reupsert_replaces_matching_ids_without_duplicates(self):
        store = LocalVectorStore(2)
        store.upsert([{"id": "a", "vector": [1.0, 0.0]}])
        store.upsert([{"id": "a", "vector": [0.0, 1.0]}, {"id": "c", "vector": [1.0, 1.0]}])
        self.assertEqual(len(store), 2)
        self.assertEqual(store.search([0.0, 1.0], top_k=1)[0][0], "a")

    def test_dimension_mismatch_is_rejected(self):
        store = LocalVectorStore(3)
        with self.assertRaises(RetrievalError):
            store.upsert([{"id": "a", "vector": [1.0, 0.0]}])

    def test_empty_store_returns_no_hits(self):
        self.assertEqual(LocalVectorStore(4).search([1.0, 0.0, 0.0, 0.0], top_k=3), [])


class MilvusScopeTest(unittest.TestCase):
    def test_scope_expression_pushes_tenant_department_and_dates(self):
        expression = MilvusVectorStore.scope_expression({
            "tenant_id": "alpha", "department_id": "engineering", "business_date": "2026-10-09"})
        self.assertIn('tenant_id == "alpha"', expression)
        self.assertIn('department_scope == "engineering"', expression)
        self.assertIn('department_scope == "*"', expression)
        self.assertIn('effective_from <= "2026-10-09"', expression)

    def test_empty_scope_produces_no_filter(self):
        self.assertEqual(MilvusVectorStore.scope_expression(None), "")
        self.assertEqual(MilvusVectorStore.scope_expression({}), "")


class HashProviderTest(unittest.TestCase):
    def test_vectors_are_normalised_and_deterministic(self):
        provider = HashEmbeddingProvider(dimension=64)
        first = provider.embed(["住宿标准"])[0]
        second = provider.embed(["住宿标准"])[0]
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(value * value for value in first), 1.0, places=5)

    def test_similar_text_scores_above_unrelated_text(self):
        provider = HashEmbeddingProvider(dimension=256)
        query = provider.embed(["异地住宿报销额度"])[0]
        rows = provider.embed(["住宿费用需凭发票报销", "潜水艇驾驶证办理流程"])
        scores = [sum(a * b for a, b in zip(query, row)) for row in rows]
        self.assertGreater(scores[0], scores[1])


class RetrieverBehaviourTest(unittest.TestCase):
    def build(self, reranker=None) -> PolicyRetriever:
        return PolicyRetriever(embedder=HashEmbeddingProvider(dimension=128), store=LocalVectorStore(128),
                               reranker=reranker, top_k=5, top_n=3)

    def test_keyword_path_when_query_is_blank(self):
        results = self.build().search(ROWS, "   ")
        self.assertEqual(results, [])

    def test_refresh_is_a_noop_until_content_changes(self):
        retriever = self.build()
        self.assertEqual(retriever.refresh(ROWS), 3)
        self.assertEqual(retriever.refresh(ROWS), 0)
        self.assertEqual(retriever.indexed_documents, 3)

    def test_ranking_never_returns_rows_outside_the_scoped_set(self):
        retriever = self.build()
        retriever.refresh(ROWS)
        scoped = [ROWS[0], ROWS[1]]
        results = retriever.search(scoped, "住宿费用报销")
        self.assertTrue({row["policy_id"] for row in results} <= {"p-1", "p-2"})

    def test_hybrid_results_are_annotated_with_the_path(self):
        retriever = self.build()
        retriever.refresh(ROWS)
        results = retriever.search(ROWS, "住宿上限")
        self.assertTrue(results)
        self.assertEqual(results[0]["retrieval_path"], retriever.mode)

    def test_rerank_stage_reorders_within_the_candidate_set(self):
        class ReverseReranker:
            name = "test-reverse"

            def rerank(self, query, texts, top_n):
                return [(index, float(len(texts) - index)) for index in range(len(texts))][:top_n]

        retriever = self.build(reranker=ReverseReranker())
        retriever.refresh(ROWS)
        results = retriever.search(ROWS, "住宿")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(row["policy_id"] in {"p-1", "p-2", "p-3"} for row in results))

    def test_mode_reflects_configured_stages(self):
        self.assertEqual(PolicyRetriever().mode, "keyword")
        self.assertEqual(self.build().mode, "hybrid")
        self.assertEqual(self.build(CrossEncoderReranker()).mode, "hybrid+rerank")

    def test_noop_reranker_is_not_treated_as_enabled(self):
        retriever = self.build(reranker=NoOpReranker())
        self.assertFalse(retriever.rerank_enabled)


class BuildRetrieverTest(unittest.TestCase):
    def test_keyword_mode_returns_none(self):
        self.assertIsNone(build_retriever("keyword", env={}))

    def test_local_backend_is_built_without_a_model_service(self):
        retriever = build_retriever("hybrid", env={"ENTERPRISE_EMBED_MODEL": "BAAI/bge-m3"})
        self.assertIsNotNone(retriever)
        self.assertEqual(retriever.mode, "hybrid")

    def test_http_backends_are_selected_from_the_environment(self):
        retriever = build_retriever("hybrid+rerank", env={
            "ENTERPRISE_EMBED_BASE": "http://127.0.0.1:8000", "ENTERPRISE_EMBED_MODEL": "BAAI/bge-m3",
            "ENTERPRISE_RERANK_BASE": "http://127.0.0.1:8080", "ENTERPRISE_RERANK_MODEL": "bge-reranker-base"})
        self.assertTrue(retriever.embedder.name.endswith("bge-m3"))
        self.assertTrue(retriever.reranker.name.endswith("bge-reranker-base"))
        self.assertEqual(retriever.store.name, "local:faiss-flat-ip" if _faiss() else "local:numpy-cosine")

    def test_milvus_backend_is_selected_from_the_environment(self):
        retriever = build_retriever("hybrid", env={
            "ENTERPRISE_EMBED_BASE": "http://127.0.0.1:8000", "ENTERPRISE_VECTOR_STORE": "milvus",
            "ENTERPRISE_MILVUS_URI": "http://milvus:19530", "ENTERPRISE_MILVUS_COLLECTION": "policies"})
        self.assertTrue(retriever.store.name.startswith("milvus:"))
        self.assertEqual(retriever.store.collection, "policies")

    def test_top_k_and_top_n_come_from_configuration(self):
        retriever = build_retriever("hybrid", env={
            "ENTERPRISE_EMBED_BASE": "http://127.0.0.1:8000",
            "ENTERPRISE_RETRIEVAL_TOP_K": "40", "ENTERPRISE_RETRIEVAL_TOP_N": "5"})
        self.assertEqual((retriever.top_k, retriever.top_n), (40, 5))


def _faiss() -> bool:
    try:
        import faiss  # noqa: F401
    except ImportError:
        return False
    return True


class ServiceIntegrationTest(unittest.TestCase):
    """The retrieval pipeline must not change what an unauthorized caller can read."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.service = EnterpriseService(Path(self.folder.name) / "app.sqlite")
        self.service.seed_demo()
        self.bob = self.service.authenticate_demo("bob")

    def tearDown(self):
        self.folder.cleanup()

    def test_keyword_mode_is_the_default_and_returns_keyword_scores(self):
        principal = self.bob
        rows = self.service.search_policies(principal, "住宿")
        self.assertTrue(rows)
        self.assertTrue(all("retrieval_score" in row for row in rows))
        self.assertTrue(all(row["retrieval_score"] > 0 for row in rows))

    def test_refresh_index_without_a_retriever_is_zero(self):
        self.assertEqual(self.service.refresh_index(), 0)

    def test_hybrid_mode_keeps_the_tenant_boundary(self):
        retriever = PolicyRetriever(embedder=HashEmbeddingProvider(dimension=128), store=LocalVectorStore(128))
        self.service.retriever = retriever
        self.assertGreater(self.service.refresh_index(), 0)
        principal = self.bob
        for row in self.service.search_policies(principal, "住宿标准"):
            self.assertEqual(row["tenant_id"], "alpha")

    def test_hybrid_mode_keeps_the_department_scope(self):
        retriever = PolicyRetriever(embedder=HashEmbeddingProvider(dimension=128), store=LocalVectorStore(128))
        self.service.retriever = retriever
        self.service.refresh_index()
        principal = self.bob
        for row in self.service.search_policies(principal, "住宿标准"):
            self.assertIn(row["department_scope"], {"engineering", "*"})

    def test_results_stay_within_the_effective_window(self):
        retriever = PolicyRetriever(embedder=HashEmbeddingProvider(dimension=128), store=LocalVectorStore(128))
        self.service.retriever = retriever
        self.service.refresh_index()
        principal = self.bob
        for row in self.service.search_policies(principal, "住宿", trip_date="2026-10-09"):
            self.assertLessEqual(row["effective_from"], "2026-10-09")
            self.assertTrue(row["effective_to"] is None or row["effective_to"] > "2026-10-09")


if __name__ == "__main__":
    unittest.main()
