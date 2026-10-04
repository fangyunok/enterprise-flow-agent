# 验证记录

日期：2026-10-04。项目版本：0.2.0。

## 已验证

- 在独立 Python 3.12 虚拟环境安装了真实 LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1、MCP 2.2.0 和锁定依赖。
- `doctor --mode fixture` 读取随附数据集，输出 `model_used=false` 记录当前执行模式。
- 命令行 `demo` 真实运行 MCP 工具和 LangGraph，关闭后重新打开引擎，恢复相同审批暂停点。
- Alice 示例原始 129000 分、可申请 123000 分、超额 6000 分；确认后创建申请编号，重复恢复返回同一编号。
- **182 项测试全部通过**：核心业务 20 项、LangGraph/MCP/模型协议 22 项、网页接口 21 项、制度问答 5 项、只读规划 24 项、多角色协作 22 项、检索链路 30 项、可观测性 17 项、API 服务 21 项。本次本机 Windows 运行耗时 268.429 秒，覆盖真实 SQLite 文件检查点与 MCP 进程内传输的全部开销。
- 只读 Agent 层在本机连续运行 30 次规划 + 30 次越权阻断 + 20 次多角色协作后，`drafts`、`confirmations`、`submissions` 三张表仍为 0 行；规划 3 次工具调用 / 上下文字符 1093，多角色协作 7 次工具调用，20 次运行得到同一个 `content_digest`。原始数据见 `results/agent_layer_benchmark.json`，脚本为 `scripts/benchmark_agent_layer.py`。
- 最终 wheel 在源码目录外的新虚拟环境中安装，通过打包数据读取、真实离线流程及 HTTP 页面检查；登录 Alice 后调用真实本地只读制度问答接口，返回授权条款预览和引用。
- wheel 内全部 Python 源码与种子 JSON 和当前 `src/enterprise_flow` 文件逐字节一致，包含最后的网页修改，并直接以独立安装包完成验证。

GitHub CI 的 Ubuntu/Windows × Python 3.11/3.12 四组检查全部通过，均完成 182 项测试、wheel 构建和源码目录外的安装运行验证。同一版本在 Ubuntu/3.12 上运行 182 项测试耗时 9.391 秒，与 Windows 本机的 268.429 秒差异来自文件系统与子进程开销，并非代码路径差异。见 [实际 CI 运行记录](https://github.com/fangyunok/enterprise-flow-agent/actions/runs/37192612775)。

CI 的 wheel 冒烟检查在安装后的独立环境中调用 `plan` 与 `collaborate`，并对网页端点 `POST /api/plan`、`POST /api/agent-proposals` 发出真实请求，断言返回 `read_only=true`、`business_effects=false`、`blocked=false`，且请求前后草稿数量不变。本次新增 `serve-api` 的真实进程检查：读取 OpenAPI 契约并核对 7 个端点路径，验证 `bob` 与 `diana` 的检索结果分别只落在 `alpha` 与 `beta` 租户，未注册身份返回 401，跨身份读取任务返回 404。日志中对应输出为 `Installed wheel read-only planning and multi-agent analysis created no business writes` 与 `Installed wheel API service, tenant isolation and cross-identity run protection smoke passed`。

## 检索质量

评测集为 168 条制度条款与 226 条查询（其中 18 条为人工撰写的同义改写问法），脚本 `scripts/evaluate_retrieval.py` 与语料生成 `scripts/build_retrieval_dataset.py`。

| 配置 | Recall@3 | Recall@5 | MRR | 越权泄漏 |
| --- | --- | --- | --- | --- |
| 关键词基线（结构化过滤 + 字符命中排序） | 0.730 | 0.898 | 0.420 | 0 |
| 混合检索（hash n-gram 编码器 + FAISS） | 0.770 | 0.885 | 0.417 | 0 |
| 混合检索（BGE-M3 稠密召回 + 关键词 RRF） | **0.836** | **0.987** | **0.450** | 0 |

三组配置的授权候选集完全一致（P50 34 条 / 最大 52 条），越权泄漏恒为 0：检索顺序的改变不扩大可读范围。`--explain N` 可打印排序失分样例，逐用例明细保留在 `results/retrieval_eval.json`。BGE-M3 在本机以 CPU 推理运行，单查询 P50 延迟 525.07 毫秒。

## 字段提取质量

标注集 27 条（`scripts/build_model_eval_dataset.py`），覆盖显式标识、隐式缺失、部分补充、身份伪造、提示注入、增量补充、格式噪声与纯弃答八类。评测脚本 `scripts/evaluate_model.py` 调用与线上相同的 `HttpExtractor`，以模型实际吐出的字段为 Precision 分母，因此凭空生成的字段同时计入假阳性与漏检；另有独立指标统计「应当留空却填了」的字段与弃答用例占比。

离线确定性解析作为基线：字段级 Precision 0.956、Recall 0.896、F1 0.925，完全匹配用例率 0.741，幻觉字段 2 个，应留空却填了 2 个，弃答用例 4/4。真实模型的同口径数字由同一脚本产出，记录在 `results/model_eval.md`。

## 服务与可观测性

- `enterprise_flow serve-api` 在本机真实启动并通过 HTTP 验证：`/health` 返回依赖就绪状态，`/policies` 对 Bob 与 Diana 分别只返回 `alpha` 与 `beta` 租户条款，未注册身份返回 401，跨身份读取任务返回 404，OpenAPI 文档包含 7 个端点。
- trace 模块的 17 项测试覆盖阶段耗时汇总、失败 span 记录、嵌套上下文恢复，以及「提供方未返回用量时单独计数而非按零计价」。
- API 的 21 项测试覆盖 OpenAPI 契约、跨租户隔离、跨身份任务拒绝、澄清与审批两个暂停点的恢复、陈旧摘要被拒（409）与决策 schema 拒绝（400/422）。

## 验证覆盖的维度

业务层覆盖员工与租户隔离、制度生效时间、金额计算与确认的确定性。MCP 与 LangGraph 层使用真实本地 SDK 与文件检查点，验证工具进程内传输、身份绑定、状态持久化与中断恢复。HTTP 层覆盖会话签名、同源保护、错误码与协议契约；API 层在此之上覆盖依赖注入、生命周期与统一错误信封。

Agent 层的验证聚焦**工程边界**：写入工具提议是否被拦下、角色是否只能调用自己的作用域、预算与循环检测是否生效、冲突分级是否正确、角色超时或异常是否被收敛、结论是否确定可复现。规划路径覆盖提示词中不出现写入工具、动作必须符合结构、越权提案被规划器阻断、用量与错误处理被记录；问答覆盖返回结构、来源 ID 与字面引用片段校验，以及 `source_preview` 模式的条款直接展示。

检索层除召回指标外，重点验证**授权先于排序**：结构化过滤产出的候选集是检索唯一可排序范围，向量库、编码器与精排器均为可替换后端，缺少模型服务时保持确定性关键词路径。金额准确率由业务测试独立评价，与检索和抽取指标分开呈现。

## 模型接入与质量评估路线

`scripts/evaluate_model.py` 已可直接对接任意 OpenAI 兼容服务，按 `model`、`base_url`、`--repeat` 与 `--show-failures` 产出逐配置对比。后续把字段级 P/R、幻觉字段数与弃答率设为 CI 阈值，使模型或提示词变更导致质量退化时在流水线被直接拦住；检索侧同法接入 Recall@3 / Recall@5 与越权泄漏计数。
