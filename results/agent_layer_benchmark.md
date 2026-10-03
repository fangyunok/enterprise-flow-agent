# Agent 层本机测量记录

数据来源：`scripts/benchmark_agent_layer.py`，脚本本身对每条结论都做了断言，运行不通过就不会产出记录。

- 采集时间：2026-10-03
- 环境：Windows 11、CPython 3.12.14、单进程本地 SQLite（WAL）、合成演示数据
- 模式：`fixture`（不调用任何大模型）

**这些是本机单次运行的墙钟耗时，不是吞吐量或线上容量结论。** 计时包含 MCP 进程内传输、SQLite 事务与 LangGraph 之外的全部编排开销，不含模型推理。

## 只读 ReAct 规划循环

| 指标 | 值 |
| --- | --- |
| 运行次数 | 30 |
| 停止原因 | `goal_satisfied`（每次一致） |
| 每次工具调用数 | 3（`get_my_orders` → `search_policy` → `calculate_expense`） |
| 回注上下文字符数 | 1093（30 次完全相同） |
| 墙钟 P50 | 555.71 ms |
| 墙钟 P95 | 676.71 ms |
| 不同结果载荷数 | 1 |

上下文字符数恒定说明循环把每次观察压缩成了固定大小的摘要 + 摘要值，没有把原始载荷反复堆进上下文。

## 越权写入阻断

规划器面对一个**只提议写入工具**的 reasoner（每次返回 `submit_expense`）：

| 指标 | 值 |
| --- | --- |
| 运行次数 | 30 |
| 停止原因 | `blocked_tool`（每次一致） |
| 实际执行的工具调用 | **0** |
| 墙钟 P50 | 33.34 ms |
| 墙钟 P95 | 38.53 ms |

耗时只有规划正常路径的约 6%，因为调用在执行前就被白名单拦下——**没有发生任何 MCP 调用**。

## Supervisor 多角色协作

| 指标 | 值 |
| --- | --- |
| 运行次数 | 20 |
| 角色数 | 3（`order_reconciler` / `policy_researcher` / `cost_estimator`） |
| 每次工具调用数 | 7 |
| 墙钟 P50 | 918.60 ms |
| 墙钟 P95 | 1122.26 ms |
| 不同 `content_digest` 数 | 1 |

相同输入在 20 次运行中得到同一个内容摘要，说明这一层是确定性的：它不依赖采样、不依赖模型、不依赖字典遍历顺序。

## 业务写入

80 次测量运行之后：

| 表 | 行数 |
| --- | --- |
| `drafts` | 0 |
| `confirmations` | 0 |
| `submissions` | 0 |

同时脚本断言了两条静态不变量：

- `planning_allowlist_excludes_write_tools`：规划白名单与写入工具集合交集为空
- `agent_scopes_exclude_write_tools`：三个角色的工具箱与写入工具集合交集为空

## 复现方式

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_agent_layer.py
```

脚本在临时目录中新建数据库，不读写项目内的 `runs/`。更换机器或 Python 版本后请重新测量，不要沿用本文件中的数字。
