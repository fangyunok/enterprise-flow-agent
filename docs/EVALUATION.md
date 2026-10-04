# 验证记录

日期：2026-10-03。项目版本：0.1.0。

## 已验证

- 在独立 Python 3.12 虚拟环境安装了真实 LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1、MCP 2.2.0 和锁定依赖。
- `doctor --mode fixture` 读取随附数据集，输出 `model_used=false` 记录当前执行模式。
- 命令行 `demo` 真实运行 MCP 工具和 LangGraph，关闭后重新打开引擎，恢复相同审批暂停点。
- Alice 示例原始 129000 分、可申请 123000 分、超额 6000 分；确认后创建申请编号，重复恢复返回同一编号。
- **114 项测试全部通过**：核心业务 20 项、LangGraph/MCP/模型协议 22 项、网页接口 21 项、制度问答 5 项、只读规划 24 项、多角色协作 22 项。本次本机 Windows 运行耗时 262.811 秒，覆盖真实 SQLite 文件检查点与 MCP 进程内传输的全部开销。
- 只读 Agent 层在本机连续运行 30 次规划 + 30 次越权阻断 + 20 次多角色协作后，`drafts`、`confirmations`、`submissions` 三张表仍为 0 行；规划 3 次工具调用 / 上下文字符 1093，多角色协作 7 次工具调用，20 次运行得到同一个 `content_digest`。原始数据见 `results/agent_layer_benchmark.json`，脚本为 `scripts/benchmark_agent_layer.py`。
- 最终 wheel 在源码目录外的新虚拟环境中安装，通过打包数据读取、真实离线流程及 HTTP 页面检查；登录 Alice 后调用真实本地只读制度问答接口，返回授权条款预览和引用。
- wheel 内全部 Python 源码与种子 JSON 和当前 `src/enterprise_flow` 文件逐字节一致，包含最后的网页修改，并直接以独立安装包完成验证。

GitHub CI 的 Ubuntu/Windows × Python 3.11/3.12 四组检查全部通过，均完成 114 项测试、wheel 构建和源码目录外的安装运行验证。同一提交 `ddf3d1f` 在 Ubuntu/3.12 上运行 114 项测试耗时 8.090 秒，与 Windows 本机的 262.811 秒差异来自文件系统与子进程开销，并非代码路径差异。见 [实际 CI 运行记录](https://github.com/fangyunok/enterprise-flow-agent/actions/runs/37118933475)。

CI 的 wheel 冒烟检查在安装后的独立环境中另外调用了 `plan` 与 `collaborate`，并对网页端点 `POST /api/plan`、`POST /api/agent-proposals` 发出真实请求，断言返回 `read_only=true`、`business_effects=false`、`blocked=false`，且请求前后草稿数量不变。日志中对应输出为 `Installed wheel read-only planning and multi-agent analysis created no business writes`。

## 验证覆盖的维度

业务层覆盖员工与租户隔离、制度生效时间、金额计算与确认的确定性。MCP 与 LangGraph 层使用真实本地 SDK 与文件检查点，验证工具进程内传输、身份绑定、状态持久化与中断恢复。HTTP 层覆盖会话签名、同源保护、错误码与协议契约。

Agent 层的验证聚焦**工程边界**：写入工具提议是否被拦下、角色是否只能调用自己的作用域、预算与循环检测是否生效、冲突分级是否正确、角色超时或异常是否被收敛、结论是否确定可复现。规划路径覆盖提示词中不出现写入工具、动作必须符合结构、越权提案被规划器阻断、用量与错误处理被记录；问答覆盖返回结构、来源 ID 与字面引用片段校验，以及 `source_preview` 模式的条款直接展示。

## 模型接入与质量评估路线

服务连通后，在 `qwen` / `api` 模式下分别测日期、订单 ID、成本中心、缺失字段和恶意身份字段，记录模型名、请求模式、真实 token 用量（提供方返回时）、输入、预期字段、实际字段与失败原因。金额准确率由业务测试独立评价，与模型提取指标分开呈现。

计划固定 20–30 条人工审核输入，据此给出字段级 precision/recall、任务到达正确暂停点的比例与越权阻断率，并把这组指标作为模型模式的质量基线随版本更新。
