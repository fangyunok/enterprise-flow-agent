#!/usr/bin/env bash
# Run the full evaluation suite against the services started by docker-compose.yml.
#
#   bash deploy/gpu/run_evaluation.sh
#
# Produces two reports, each holding one section per configuration so the before/after numbers sit
# in a single file:
#   results/retrieval_eval.md   keyword baseline vs hybrid recall vs hybrid+rerank
#   results/model_eval.md       field-extraction precision/recall against the labelled set
#
# Override the model service with QWEN_BASE / QWEN_MODEL / QWEN_KEY if it is not on the local port.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv}"
if [[ -x "$VENV/bin/python" ]]; then PYTHON="$VENV/bin/python"; fi

EMBED_BASE="${EMBED_BASE:-http://127.0.0.1:8000}"
RERANK_BASE="${RERANK_BASE:-http://127.0.0.1:8080}"
MILVUS_URI="${MILVUS_URI:-http://127.0.0.1:19530}"
EMBED_MODEL="${EMBED_MODEL:-BAAI/bge-m3}"
RERANK_MODEL="${RERANK_MODEL:-BAAI/bge-reranker-base}"
DEVICE="${DEVICE:-cuda}"

echo "== 0. dependency check =="
"$PYTHON" - <<'PY'
import importlib, sys
missing = [name for name in ("httpx", "pymilvus") if importlib.util.find_spec(name) is None]
if missing:
    sys.exit(f"missing: {', '.join(missing)} — run: pip install -r deploy/gpu/requirements.txt")
print("dependencies ok")
PY

echo "== 1. datasets =="
"$PYTHON" scripts/build_retrieval_dataset.py
"$PYTHON" scripts/build_model_eval_dataset.py

echo "== 2. wait for services =="
wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 60); do
    if curl -sf "$url" >/dev/null 2>&1; then echo "  $name ready"; return 0; fi
    sleep 5
  done
  echo "  $name did not become ready: $url" >&2; return 1
}
wait_for "bge-m3" "$EMBED_BASE/health"
wait_for "bge-reranker" "$RERANK_BASE/health"

echo "== 3. retrieval: keyword baseline =="
"$PYTHON" scripts/evaluate_retrieval.py --mode keyword \
  --label "A. 关键词基线（结构化过滤 + 字符命中排序）"

echo "== 4. retrieval: hybrid (BGE-M3 dense + keyword RRF) =="
"$PYTHON" scripts/evaluate_retrieval.py --mode hybrid --append \
  --embedder local --embed-model "$EMBED_MODEL" --device "$DEVICE" \
  --vector-store milvus --milvus-uri "$MILVUS_URI" \
  --label "B. 混合检索（BGE-M3 稠密召回 + 关键词 RRF，Milvus）"

echo "== 5. retrieval: hybrid + cross-encoder rerank =="
"$PYTHON" scripts/evaluate_retrieval.py --mode hybrid+rerank --append \
  --embedder local --embed-model "$EMBED_MODEL" --device "$DEVICE" \
  --vector-store milvus --milvus-uri "$MILVUS_URI" \
  --reranker http --rerank-base "$RERANK_BASE" --rerank-model "$RERANK_MODEL" \
  --label "C. 混合检索 + bge-reranker 精排（上线配置）"

echo "== 6. model extraction: offline baseline =="
"$PYTHON" scripts/evaluate_model.py --mode fixture \
  --label "A. 离线确定性解析（基线，非模型）"

if [[ -n "${QWEN_BASE:-}" ]]; then
  echo "== 7. model extraction: $QWEN_MODEL =="
  "$PYTHON" scripts/evaluate_model.py --mode api --append \
    --base-url "$QWEN_BASE" --model "$QWEN_MODEL" --api-key "${QWEN_KEY:-}" \
    --concurrency "${QWEN_CONCURRENCY:-4}" --repeat "${QWEN_REPEAT:-3}" \
    --show-failures 10 \
    --label "B. 真实模型字段提取（$QWEN_MODEL）"
else
  echo "== 7. model extraction: skipped (set QWEN_BASE / QWEN_MODEL to enable) =="
fi

echo
echo "reports: results/retrieval_eval.md, results/model_eval.md"
