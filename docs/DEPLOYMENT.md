# 部署与交付

三种交付形态共用同一套业务服务、编排引擎与控制台资产：容器镜像、Python wheel、可发布部署包。

## 一、容器（推荐给演示环境）

```bash
docker compose up --build -d
```

| 服务 | 端口 | 入口 |
| --- | --- | --- |
| `console` | 7861 | 网页控制台，同源 JSON API |
| `api` | 8000 | FastAPI 服务，`/docs` 为 OpenAPI 契约 |

镜像为多阶段构建：wheel 在 build 阶段产出，runtime 阶段只安装 wheel 与锁定依赖，并以非 root 用户运行。
业务库与检查点库落在 `enterprise-runs` 命名卷中，容器重启后运行、草稿与审计事件都还在。

单独构建镜像：

```bash
docker build -t enterprise-flow-agent:0.3.0 .
docker run --rm -p 7861:7861 enterprise-flow-agent:0.3.0
docker run --rm -p 8000:8000 enterprise-flow-agent:0.3.0 \
  python -m enterprise_flow serve-api --host 0.0.0.0 --port 8000 --mode fixture
```

## 二、wheel（推荐给集成方）

```bash
python -m build --wheel
python -m pip install dist/enterprise_flow_agent-0.3.0-py3-none-any.whl
enterprise-flow --help
enterprise-flow doctor --mode fixture
enterprise-flow serve --db runs/web.sqlite --mode fixture
```

wheel 随附内置数据集、控制台资产与评测摘要，安装后不依赖源码目录即可运行。CI 会校验 wheel 内的
源文件与源码树逐字节一致，并脱离源码目录跑通 `demo` / `plan` / `collaborate` / `serve` / `serve-api`。

## 三、可发布部署包（单端口 HTTP 服务）

发布沙箱只上传一个目录，因此部署包把包的副本与入口文件并排放在同一目录：

```bash
python scripts/build_site_bundle.py   # 产出 dist/site
cd dist/site && python server.py      # 监听 $PORT，默认 8000
```

```text
dist/site/
  server.py            单端口入口：uvicorn + create_app
  requirements.txt     运行所需的最小依赖
  enterprise_flow/     与 src/enterprise_flow 逐字节一致的副本
    web/               控制台资产（index.html / console.css / console.js）
    data/              内置数据集与评测摘要
```

生产部署请根据入口类型覆盖命令，例如 `python server.py` 或 `uvicorn server:app --host 0.0.0.0 --port $PORT`。
数据落在 SQLite 文件中，无需外部数据库、缓存或消息队列。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENTERPRISEFLOW_DB` | `runs/enterprise.sqlite` | 业务库路径；检查点库由它派生 |
| `ENTERPRISEFLOW_MODE` | `fixture` | `fixture` / `qwen` / `api` |
| `PORT` | `8000`（部署包） | 部署包监听端口 |
| `ENTERPRISE_QWEN_BASE` / `_MODEL` | — | Qwen 的 OpenAI 兼容接口 |
| `ENTERPRISE_API_BASE` / `_MODEL` / `_KEY` | — | 通用模型 API |
| `ENTERPRISE_RETRIEVAL_MODE` | `keyword` | `keyword` / `hybrid` / `hybrid+rerank` |
| `ENTERPRISE_VECTOR_STORE` | `local` | `local`（FAISS）或 `milvus` |

## 健康检查与指标

| 路径 | 用途 |
| --- | --- |
| `/health` | 进程、数据库、检索后端与编排引擎的就绪状态 |
| `/api/metrics` | Prometheus 文本指标（工具调用、运行耗时、模型调用、提交数） |
| `/api/contract` | 控制台 API 的端点清单，供集成方核对 |
| `/api/trace` | 最近一次运行的 trace：span、阶段耗时、token 与成本 |

## 上线前检查

```bash
python -m unittest discover -s tests -v   # 全部测试
python scripts/smoke_console.py           # 真实端口上的控制台冒烟
python scripts/build_eval_summary.py      # 由 results/ 重生成打包的评测摘要
python scripts/build_site_bundle.py       # 产出部署包
```
