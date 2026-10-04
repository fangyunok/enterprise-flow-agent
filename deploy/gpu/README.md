# GPU 部署与评测

检索与模型质量评测在带 GPU 的 Linux 机器上执行：Milvus 提供向量索引，两侧车提供 BGE-M3 稠密
检索与 bge-reranker 精排，Qwen 通过 OpenAI 兼容接口接入。本地（无 GPU）仍可用 FAISS +
sentence-transformers 或哈希编码器跑通同一条链路。

## 前置条件

| 组件 | 要求 |
| --- | --- |
| Docker / Docker Compose | 24.0+ |
| NVIDIA 驱动 + Container Toolkit | 需 `nvidia-smi` 与 `docker run --gpus all` 均可用 |
| 磁盘可用空间 | 30 GB（Milvus 约 8 GB，模型权重约 6 GB，容器镜像约 10 GB） |
| 显存 | 8 GB 起；BGE-M3 与 reranker 各占约 2–3 GB |

无 GPU 时把 compose 文件里两侧车的 `deploy.resources` 段删掉，评测改用本地后端：

```bash
DEVICE=cpu VECTOR_STORE=local python scripts/evaluate_retrieval.py --mode hybrid+rerank \
  --embedder local --reranker local
```

## 一、起服务

```bash
cd enterprise-flow-agent
docker compose -f deploy/gpu/docker-compose.yml up -d
```

首次拉取镜像与模型权重约需 10–20 分钟。确认四个服务就绪：

```bash
curl -sf http://127.0.0.1:8000/health && echo " bge-m3 ok"
curl -sf http://127.0.0.1:8080/health && echo " reranker ok"
curl -sf http://127.0.0.1:9091/healthz && echo " milvus ok"
```

## 二、起 Qwen

用 vLLM 起一个 OpenAI 兼容服务（吞吐优于 Ollama，适合批量评测）：

```bash
pip install vllm
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --port 8001 --max-model-len 8192 --gpu-memory-utilization 0.85 --dtype bfloat16
```

或用 Ollama：

```bash
ollama serve
ollama pull qwen3:8b
```

两种方式都暴露 `/v1/chat/completions`，与项目现有 `HttpExtractor` 兼容。

## 三、装依赖并跑评测

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r deploy/gpu/requirements.txt
pip install -e ".[dev]" 2>/dev/null || pip install langgraph langgraph-checkpoint-sqlite mcp starlette python-multipart pydantic
```

一条命令跑完全部评测：

```bash
QWEN_BASE=http://127.0.0.1:8001/v1 \
QWEN_MODEL=Qwen/Qwen2.5-7B-Instruct \
bash deploy/gpu/run_evaluation.sh
```

它依次产出三组检索配置与两组模型配置的对比数字，分别写入
`results/retrieval_eval.md` 与 `results/model_eval.md`。

单独跑某一项：

```bash
# 检索：关键词 vs 混合 vs 混合+精排
python scripts/evaluate_retrieval.py --mode hybrid+rerank --embedder local --device cuda \
  --vector-store milvus --milvus-uri http://127.0.0.1:19530 \
  --reranker http --rerank-base http://127.0.0.1:8080

# 字段提取：3 轮重复 + 失败样例
python scripts/evaluate_model.py --mode api --base-url http://127.0.0.1:8001/v1 \
  --model Qwen/Qwen2.5-7B-Instruct --repeat 3 --show-failures 10
```

## 四、结果回传

两个报告都是纯 Markdown，直接贴回对话或提交进仓库即可；需要逐用例明细时一并取
`results/*.json`，其中 `per_case` 字段记录每条用例的期望值、实际值与失分原因。

## 常见问题

**Milvus 容器反复重启** —— 内存不足。`ETCD_AUTO_COMPACTION_RETENTION` 已降到 1000，仍失败时
给 etcd/minio 各留 512 MB 以上。

**`bge-m3` health 一直不通** —— 首次启动要下载权重，数分钟内无响应属正常。若报 CUDA 错误，
确认 `nvidia-smi` 在容器内可见，或先按上文切 CPU 路线。

**评测脚本报 `missing: pymilvus`** —— 只装了 `requirements.txt` 而没装项目本体，Milvus 后端
导入失败。补装项目依赖或改用 `--vector-store local`。

**vLLM OOM** —— 调低 `--gpu-memory-utilization` 到 0.7，或减小 `--max-model-len` 到 4096。

## 与线上配置的关系

评测脚本使用的 embedder、向量库、reranker 与 `HttpExtractor` 全部来自
`src/enterprise_flow/retrieval.py` 和 `src/enterprise_flow/model.py`，与网页/CLI 走同一套代码路径，
不额外维护一份评测专用实现。配置通过环境变量注入：

| 环境变量 | 作用 | 示例 |
| --- | --- | --- |
| `ENTERPRISE_RETRIEVAL_MODE` | `keyword` / `hybrid` / `hybrid+rerank` | `hybrid+rerank` |
| `ENTERPRISE_EMBED_BASE` | 稠密检索服务地址 | `http://127.0.0.1:8000` |
| `ENTERPRISE_EMBED_MODEL` | 稠密模型名 | `BAAI/bge-m3` |
| `ENTERPRISE_VECTOR_STORE` | `local` / `milvus` | `milvus` |
| `ENTERPRISE_MILVUS_URI` | Milvus 地址 | `http://127.0.0.1:19530` |
| `ENTERPRISE_RERANK_BASE` | 精排服务地址 | `http://127.0.0.1:8080` |
| `ENTERPRISE_RERANK_MODEL` | 精排模型名 | `BAAI/bge-reranker-base` |
| `ENTERPRISE_QWEN_BASE` / `_MODEL` / `_KEY` | 字段提取模型服务 | `http://127.0.0.1:8001/v1` |
