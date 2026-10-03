# EnterpriseFlow：企业业务流程 Agent 编排与执行平台

[![EnterpriseFlow checks](https://github.com/fangyunok/enterprise-flow-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/fangyunok/enterprise-flow-agent/actions/workflows/ci.yml)

**用 LangGraph 状态图编排企业业务流程的 Agent 平台**：把任务解析、制度检索、MCP 工具调用、人工确认与事务提交组织成可持久化、可中断、可恢复的工作流。第一条示范流程使用自建数据办理**模拟差旅费用申请**。

模型只负责提取用户明确提供的字段；身份、租户、金额、制度适用范围与提交许可全部由服务端确定性业务代码裁定。`fixture` 模式可以离线完整演示，明确记录 `model_used=false`。

在此之上另有一个**只读 Agent 层**：有界 ReAct 规划循环按需调用查询与核算工具，Supervisor 再派发三个最小权限角色做订单核对、条款检索与确定性核算，并对结论做交叉检查。这一层只会调用只读工具，写入类工具在执行前就被规划器拦下——创建、确认与提交仍然只走人工确认流程。

网页同时提供只读制度问答：检索授权范围内的条款后，Qwen/API 模式生成带引用的答案，校验来源 ID 与原文片段并标记待人工核查；离线模式直接展示条款，不生成模型答案。

![EnterpriseFlow 本地业务工作台：本人订单、费用核算与适用条款](docs/assets/workbench.png)

截图来自真实本地网页的审批暂停点，使用合成数据和固定流程模式。

## 先看结论

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| 是否使用真实 Agent 编排框架 | LangGraph 1.2.12 真实 `StateGraph`；字段补充与审批通过 `interrupt` / `Command` 暂停与恢复，不是 if/else 流水线 | [架构](docs/ARCHITECTURE.md) |
| 中断的任务能否恢复 | `AsyncSqliteSaver` 文件检查点，进程重启后按同一 `run_id` 读回原暂停点继续；`demo` 命令重复恢复后申请编号保持一致 | [验证记录](docs/EVALUATION.md) |
| 模型能否越权 | 模型可输出字段仅限订单 ID、成本中心、日期、目的地、备注；身份、租户、金额与制度适用由服务端判定，MCP 工具服务器绑定服务端身份，参数中不含员工或租户字段 | [架构](docs/ARCHITECTURE.md) |
| 旧确认与重复提交如何处理 | 草稿绑定版本号与 SHA256 内容摘要，编辑草稿或来源变化后旧确认立即失效；写事务 + 唯一约束 + 幂等键，重复提交返回同一编号 | [架构](docs/ARCHITECTURE.md) |
| 记忆机制如何分层 | 长期偏好（用户显式保存的成本中心跨任务复用）与单任务短期状态分离；当前显式字段优先于已存偏好，偏好复用前再通过一次权限检查 | [架构](docs/ARCHITECTURE.md) |
| Agent 能否自己决定调什么工具 | 可以，但在只读白名单内：有界 ReAct 循环按需查询与核算，步数、上下文与循环检测同时生效；对写入工具的提议在执行前被拦下，实测 30 次全部 `blocked_tool` 且工具调用为 0 | [Agent 层](docs/AGENT_LAYER.md) · [测量记录](results/agent_layer_benchmark.md) |
| 多角色结论冲突时听谁的 | 三个最小权限角色分别产出结论，Supervisor 做交叉检查并按 blocking / warning 分级；条款同日多版本、制度缺口、订单不符合条件都会阻塞，订单数不一致只告警并以业务服务为准 | [Agent 层](docs/AGENT_LAYER.md) |
| 结果是否可验证 | 114 项测试（业务 20 / 编排 22 / 网页 21 / 问答 5 / 只读规划 24 / 多角色 22）+ Ubuntu·Windows × Python 3.11·3.12 四组 CI 全部通过 + 脱离源码目录的 wheel 安装与 HTTP 检查 | [验证记录](docs/EVALUATION.md) · [CI 运行记录](https://github.com/fangyunok/enterprise-flow-agent/actions/runs/37106022955) |

> **能力边界**：真实 Qwen 端到端字段提取质量与制度问答的语义支持尚未验证；模型驱动的规划路径只做了协议层验证，未做端到端质量评测；制度检索为关键词排序 + 结构化过滤，不是向量 RAG；演示身份、制度与订单均为自建合成数据。离线结果不代表生产环境表现。

## 五分钟运行

需要 Python 3.11 或 3.12。在项目目录执行：

```powershell
git clone https://github.com/fangyunok/enterprise-flow-agent.git
cd enterprise-flow-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m enterprise_flow doctor --mode fixture
.\.venv\Scripts\python.exe -m enterprise_flow demo --db runs/demo.sqlite --mode fixture
.\.venv\Scripts\python.exe -m enterprise_flow plan --db runs/demo.sqlite --mode fixture
.\.venv\Scripts\python.exe -m enterprise_flow collaborate --db runs/demo.sqlite --mode fixture
.\.venv\Scripts\python.exe -m enterprise_flow serve --db runs/web.sqlite --mode fixture
```

Linux/macOS 使用 `.venv/bin/python` 替换上述解释器路径。打开 `http://127.0.0.1:7861`，选择演示员工 Alice，再输入：

> 我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，成本中心 CC-ALPHA-OPS。

示例住宿为 860 元、两晚，制度上限每晚 400 元；铁路订单 430 元。草稿显示原始金额 **1290 元**、可申请金额 **1230 元**、超额 **60 元**，并列出条款版本。只有确认当前草稿后才创建模拟申请编号。没有付款、真实财务审批或外部企业连接。

命令行 `demo` 会关闭并重新打开 LangGraph 引擎，读取同一审批检查点，再确认提交，最后重复恢复一次并验证申请编号一致。

`plan` 打印只读规划轨迹：每一步的工具、参数、观察摘要，以及停止原因、工具调用次数与回注上下文字符数。`collaborate` 打印三个角色的结论、交叉检查结果、候选条款和 `content_digest`，并附带 `draft_count_after_analysis`。两条命令都不产生业务写入。

## 工程能力

| 能力 | 实现 |
| --- | --- |
| 流程编排 | 真实 LangGraph 状态图，字段补充和审批使用 `interrupt`/`Command` |
| 持久化恢复 | `AsyncSqliteSaver` 文件检查点，业务库与检查点库分开 |
| MCP 集成 | 官方 SDK 内存传输；每个工具服务器绑定服务端身份，不接受模型传入员工或租户身份 |
| 制度检索 | SQL 先按租户、部门和生效日期过滤，再进行关键词排序；计算结果引用确定适用的条款 |
| 制度问答 | MCP 检索后读取模型 JSON 答复，验证回答片段和来源片段；问答不执行业务写操作 |
| 费用核算 | 金额使用整数分；住宿按晚拆分并选择当日制度；冲突或证据缺失时阻止办理 |
| 版本确认 | 草稿版本与 SHA256 内容摘要绑定；编辑草稿或来源变化后旧确认失效 |
| 事务提交 | SQLite 写事务、唯一约束和幂等键；重复提交返回同一编号 |
| 长期偏好 | 用户明确保存的成本中心可跨任务复用；当前字段优先，支持修改和删除 |
| 只读规划循环 | 有界 ReAct：步数 / 上下文字符 / 循环签名三重限制；观察只以摘要回注，写入工具在执行前被白名单拦下 |
| 多角色协作 | Supervisor + 三个最小权限角色，黑板交接订单范围，结论交叉检查并按 blocking / warning 分级 |
| 可复现测量 | `scripts/benchmark_agent_layer.py` 输出 P50 / P95、工具调用数与业务写入计数，脚本内断言与结论同时成立 |
| 网页与接口 | Starlette 同源写请求保护、签名演示会话、来源对照和任务恢复 |
| 交付 | 打包种子数据、锁定依赖、跨系统 CI、脱离源码目录的 wheel 演示与 HTTP 检查 |

```mermaid
flowchart LR
  A[用户任务] --> B[受限字段提取]
  B --> C{字段齐全?}
  C -- 否 --> D[持久化暂停并补充字段]
  D --> C
  C -- 是 --> E[MCP 查询本人订单与制度]
  E --> F[确定性核算与草稿]
  F --> G[持久化等待人工确认]
  G --> H[校验版本与来源快照]
  H --> I[事务与幂等提交]
```

## 模型配置

Qwen 和可选 API 都使用 OpenAI 兼容 HTTP 接口。配置读取**进程环境变量**，不会自动加载 `.env`；`.env.example` 仅提供字段参考。

```powershell
$env:ENTERPRISE_QWEN_BASE = 'http://127.0.0.1:11435/v1'
$env:ENTERPRISE_QWEN_MODEL = 'qwen3:4b-instruct'
.\.venv\Scripts\python.exe -m enterprise_flow doctor --mode qwen
.\.venv\Scripts\python.exe -m enterprise_flow serve --db runs/qwen.sqlite --mode qwen
```

`doctor` 只验证目录接口中是否有指定模型，不能代表字段提取质量已通过。真实模型不可用时返回失败，不自动切换为离线成功结果。API 模式使用 `ENTERPRISE_API_BASE`、`ENTERPRISE_API_MODEL`、`ENTERPRISE_API_KEY`。

## 验证与项目范围

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m build --wheel
```

**114 项测试全部通过**，覆盖身份隔离、制度版本、金额计算、旧确认失效、并发及重复提交、真实 MCP 调用、LangGraph 检查点恢复、HTTP 会话、制度问答引用、只读规划循环的预算与越权阻断，以及多角色的作用域约束与冲突检测。Ubuntu/Windows × Python 3.11/3.12 四组 CI 已全部通过，见 [实际运行记录](https://github.com/fangyunok/enterprise-flow-agent/actions/runs/37106022955)。验证范围与真实模型限制见 [EVALUATION.md](docs/EVALUATION.md)。

本版是单机工程演示，使用合成制度、员工和订单。演示登录允许选择模拟身份，不构成正式企业认证。制度检索使用关键词与结构化过滤，未接入向量数据库；Agent 层的路由依据已抽取字段而非模型自主编排，规划与协作都只能读；模型驱动的规划路径只做了协议层验证。

## 目录与后续阅读

```text
src/enterprise_flow/
  database.py       数据库结构与事务
  schemas.py        严格字段与身份契约
  service.py        授权、制度、核算、草稿与提交
  model.py          离线解析与真实模型字段提取
  policy_qa.py      只读制度问答与引用核验
  planner.py        有界只读 ReAct 规划循环
  agents.py         Supervisor 多角色协作与交叉检查
  tools.py          身份绑定的 MCP 工具
  workflow.py       LangGraph 编排与恢复
  webapp.py         网页、会话和 HTTP API
  cli.py            seed/demo/plan/collaborate/serve/doctor 命令
  data/             wheel 随附公开合成数据
tests/              业务和跨层边界测试
data/               可阅读的源数据副本
scripts/            可复现的 Agent 层测量脚本
results/            Agent 层测量记录
docs/               架构、Agent 层、评测和投递材料
```

- [架构与失败处理](docs/ARCHITECTURE.md)
- [Agent 层：只读规划与多角色协作](docs/AGENT_LAYER.md)
- [验证记录](docs/EVALUATION.md)
- [GitHub 发布与运行](docs/GITHUB_SETUP.md)
- [简历与面试说明](docs/RESUME_ENTRY.md)

MIT 许可覆盖本仓库代码和自建合成数据，第三方依赖遵循各自许可证。
