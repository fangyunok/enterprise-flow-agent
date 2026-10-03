# 验证记录

日期：2026-10-03。项目版本：0.1.0。

## 已验证

- 在独立 Python 3.12 虚拟环境安装了真实 LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1、MCP 2.2.0 和锁定依赖。
- `doctor --mode fixture` 读取随附合成数据，记录 `model_used=false`。
- 命令行 `demo` 真实运行 MCP 工具和 LangGraph，关闭后重新打开引擎，恢复相同审批暂停点。
- Alice 示例原始 129000 分、可申请 123000 分、超额 6000 分；确认后创建模拟申请编号，重复恢复返回同一编号。
- **114 项测试全部通过**：核心业务 20 项、LangGraph/MCP/模型协议 22 项、网页接口 21 项、制度问答 5 项、只读规划 24 项、多角色协作 22 项。本次本机 Windows 运行耗时 262.811 秒，包含真实 SQLite 文件检查点与 MCP 进程内传输的开销；这是本机测试记录，不是业务吞吐量。
- 只读 Agent 层在本机压测 30 次规划 + 30 次越权阻断 + 20 次多角色协作后，`drafts`、`confirmations`、`submissions` 三张表仍为 0 行；规划 3 次工具调用 / 上下文字符 1093，多角色协作 7 次工具调用，20 次运行得到同一个 `content_digest`。原始数据见 `results/agent_layer_benchmark.json`，脚本为 `scripts/benchmark_agent_layer.py`。
- 最终 wheel 在源码目录外的新虚拟环境中安装，通过打包数据读取、真实离线流程及 HTTP 页面检查；登录 Alice 后调用真实本地只读制度问答接口，返回授权条款预览和引用。
- wheel 内全部 Python 源码与种子 JSON 和当前 `src/enterprise_flow` 文件逐字节一致，包含最后的网页修改。未复用源码目录或 editable 安装执行独立安装检查。

GitHub CI 的 Ubuntu/Windows × Python 3.11/3.12 四组检查全部通过，均完成 114 项测试、wheel 构建和源码目录外的安装运行验证。同一提交 `ddf3d1f` 在 Ubuntu/3.12 上运行 114 项测试耗时 8.090 秒，与 Windows 本机的 262.811 秒差异来自文件系统与子进程开销，并非代码路径差异。见 [实际 CI 运行记录](https://github.com/fangyunok/enterprise-flow-agent/actions/runs/37118933475)。该记录没有连接真实 Qwen。

CI 的 wheel 冒烟检查在安装后的独立环境中另外调用了 `plan` 与 `collaborate`，并对网页端点 `POST /api/plan`、`POST /api/agent-proposals` 发出真实请求，断言返回 `read_only=true`、`business_effects=false`、`blocked=false`，且请求前后草稿数量不变。日志中对应输出为 `Installed wheel read-only planning and multi-agent analysis created no business writes`。

## 测试评价的范围

业务测试检查员工与租户隔离、制度生效时间、金额和确认的确定性。MCP 和 LangGraph 测试使用真实本地 SDK 与文件检查点，但 `fixture` 解析不是大模型调用。HTTPX 模拟响应只用于失败处理和协议契约检查。

Agent 层测试检查的是**边界而不是智能**：写入工具提议是否被拦下、角色是否只能调用自己的作用域、预算与循环检测是否生效、冲突分级是否正确、角色超时或异常是否被收敛、结论是否确定可复现。所有确定性指标都在 `fixture` 模式下取得，不含任何模型调用。

真实 Qwen 端到端调用、自然语言字段提取质量、模型驱动规划的动作质量及制度问答语义支持均尚未验证。规划路径只验证了提示词中不出现写入工具、动作必须符合结构、越权提案被规划器阻断、用量与错误处理被记录。问答只校验返回结构、来源 ID 和字面引用片段，不把该校验当成答案正确性的证明。离线制度问答直接展示检索条款并标记 `source_preview`。没有宣称自然语言成功率、生产吞吐量、企业应用用户规模或真实业务收益。

## 投递前的真实模型验证

服务连通后分别测日期、订单 ID、成本中心、缺失字段和恶意身份字段。记录模型名、请求模式、真实 token 用量（提供方返回时）、输入、预期字段、实际字段和失败原因。金额准确率应由业务测试评价，不能与模型提取准确率混合。

建议先固定 20–30 条人工审核输入，再评估字段级 precision/recall、任务到达正确暂停点的比例和越权阻断。未经独立标注和实际执行，不能把建议指标写成项目成绩。
