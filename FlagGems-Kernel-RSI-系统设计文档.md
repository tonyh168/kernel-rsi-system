# FlagGems Kernel 自动迭代优化 RSI 系统设计文档

## 1. 设计目标

打造一套可以自动迭代优化 FlagGems Kernel 的实验驱动系统，主要架构为：

- Python Driver 脚本负责确定性调度、状态管理、预算、Git 隔离和最终验收。
- Claude Agent SDK 驱动一个主 Session，负责架构分析、研究规划和跨轮决策。
- 主 Session 调用专业子 Agent，完成代码研究、性能分析和候选实现。
- 通过正确性测试、性能基准和回归评测决定候选是否晋级。

建议将系统实现为 **“Python 驱动的实验闭环 + 主 Session 研究规划 + 子 Agent 分工 + 确定性评测裁决”**，而不是让模型在一个 `while True` 中自由修改代码。

这里的 RSI 是实验驱动的演化式自动改进，不涉及修改模型权重。

## 2. 当前环境与技术路线

设计时已检查本机环境：

- 当前项目目录为空，可以直接初始化新工程。
- 已安装 `claude-agent-sdk==0.2.183`。
- 当前 SDK 支持主 Session、子 Agent、结构化输出、MCP 工具和预算限制。
- 控制机为 macOS，建议从一开始将 Agent 控制面与硬件执行面分离。

该设计采用 Claude Agent SDK，而不是自行实现底层工具调用循环。SDK 负责 Agent 循环、工具调用、上下文和 Session；Driver 负责外层实验状态机与硬件任务生命周期。

## 3. 总体架构

```text
                    ┌─────────────────────────┐
                    │      Python Driver      │
                    │ 状态机 / 预算 / Git      │
                    │ 任务调度 / 验收 / 断点恢复 │
                    └────────────┬────────────┘
                                 │
                     IterationContext + 历史摘要
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  主 Session：研究员/架构师 │
                    │ 选择算子、提出假设、制定计划 │
                    │ 不直接执行任意 GPU 命令     │
                    │ 不拥有最终验收权            │
                    └──────┬──────┬──────┬─────┘
                           │      │      │
                 ┌─────────┘      │      └──────────┐
                 ▼                ▼                 ▼
        ┌────────────────┐ ┌──────────────┐ ┌──────────────────┐
        │ code-researcher │ │ profiler     │ │ kernel-developer │
        │ 阅读算子/测试    │ │ 分析瓶颈      │ │ 在独立 worktree   │
        │ 提出优化假设     │ │ 汇总性能数据  │ │ 实现候选优化      │
        └────────────────┘ └──────────────┘ └──────────────────┘
                 │                │                 │
                 └────────────────┴────────┬────────┘
                                          ▼
                              ┌───────────────────────┐
                              │ 非 LLM Evaluator      │
                              │ 正确性 → 性能 → 回归   │
                              │ ACCEPT/REJECT/ERROR   │
                              └───────────┬───────────┘
                                          ▼
                            实验数据库 + Champion + 原始日志
```

### 3.1 核心边界

- Agent 负责提出假设、分析证据和生成 patch。
- Python Driver 负责运行状态、资源调度、代码隔离和实验记录。
- Evaluator 负责最终验收，模型声称“提升了 20%”不作为验收证据。
- 主 Session 可以保留研究上下文，但磁盘状态才是事实来源。
- 第一版冻结 Driver、测试集和评测脚本，禁止 Agent 修改，避免系统优化评测器而不是优化 Kernel。

## 4. Driver 与迭代状态机

```text
PREFLIGHT
   ↓
BASELINE
   ↓
PLAN
   ↓
RESEARCH / PROFILE
   ↓
IMPLEMENT
   ↓
CORRECTNESS
   ↓
BENCHMARK
   ↓
REGRESSION
   ↓
ACCEPT ──────────→ 更新 Champion
   │
   └→ REJECT / INFRA_ERROR / BUDGET_EXHAUSTED
   ↓
下一轮或停止
```

### 4.1 每轮输入

```json
{
  "champion_revision": "...",
  "target_device": "...",
  "target_operators": ["..."],
  "baseline_summary": {},
  "recent_successes": [],
  "recent_failures": [],
  "remaining_budget": {},
  "constraints": {
    "allowed_files": [],
    "max_candidates": 1
  }
}
```

### 4.2 主 Agent 的结构化输出

主 Agent 通过 `output_format` 返回结构化计划，Driver 使用 Pydantic 模型验证后执行，不解析自然语言中的“我打算修改某某文件”。

```json
{
  "hypothesis": "减少某类 shape 下的额外内存访问",
  "target_operator": "...",
  "target_shapes": [],
  "expected_bottleneck": "...",
  "implementation_steps": [],
  "validation_requirements": [],
  "risk": "...",
  "stop_condition": "..."
}
```

### 4.3 主 Session 的生命周期

- 进程内使用 `ClaudeSDKClient`，多轮调用自动延续同一 Session。
- 保存 `ResultMessage.session_id`，进程崩溃后可以通过 `resume` 恢复。
- 每隔若干轮，根据磁盘上的实验摘要开启新 Session，避免上下文无限增长。
- 历史摘要重点保留：尝试过什么、为什么失败、哪些方向重复、哪些优化可以组合。
- 不把所有原始 profile 和编译日志重新塞回主 Session。

## 5. 子 Agent 分工

第一版建议只设置 3 类，避免过早拆分：

| 子 Agent | 权限 | 职责 |
|---|---|---|
| `code-researcher` | Read/Grep/Glob | 阅读 Kernel、dispatch 和测试，提出可证伪的优化假设 |
| `profiler` | 只读文件和受控 profiling 工具 | 运行或读取 profile，输出瓶颈摘要，不修改代码 |
| `kernel-developer` | 指定 worktree 内编辑和测试工具 | 根据计划实现 patch，解释修改和验证结果 |

后续可以增加 `reviewer`，专门检查越界、race、dtype/layout 遗漏和性能结论是否可靠。

### 5.1 子 Agent 的约束

- 子 Agent 默认不会继承主 Session 的对话历史，调用时必须传入具体文件路径、实验目录、候选 revision 和限制条件。
- 子 Agent 的 `tools` 才真正限制工具集合；`allowed_tools` 只是自动批准，不是工具白名单。
- 子 Agent 不能无限派生子 Agent，建议最大深度设为 `1`。
- 分析类 Agent 可以并行 2～4 个，但 GPU profiling/benchmark 初始设置为每卡一个任务。
- 子 Agent 只返回简洁报告，中间工具调用和大量日志不进入主 Session。

## 6. 受控 GPU 工具与执行层

不要给主 Agent 一个可以执行任意字符串的远程 Bash。使用 SDK MCP 暴露领域工具：

```text
get_experiment_context(run_id)
get_candidate_diff(candidate_id)
submit_correctness(candidate_id, suite, seed)
submit_profile(candidate_id, workload_id)
submit_benchmark(candidate_id, benchmark_id)
get_job_status(job_id)
get_job_summary(job_id)
```

### 6.1 参数与输出约束

- 工具参数使用 `candidate_id/workload_id`，不接受任意路径和任意 shell command。
- 长任务采用 `submit → job_id → Driver 调度/等待 → get_job_summary`。
- 原始日志写入文件，只给 Agent 返回摘要和日志路径。
- Driver 只信任结构化结果文件，不信任模型自行报告的成功或性能数字。

### 6.2 Runner 抽象

```python
class Runner(Protocol):
    async def submit(self, job: JobSpec) -> str: ...
    async def status(self, job_id: str) -> JobStatus: ...
    async def collect(self, job_id: str) -> JobResult: ...
```

先实现一种 Runner 即可：

- `LocalRunner`：本机有目标加速卡时使用。
- `DockerRunner`：推荐，用于隔离依赖和生成代码。
- `SSHRunner`：用于远程 Linux GPU/NPU 机器，但只调用服务器上的固定任务入口。

当前控制机为 macOS，应将 SDK 控制进程和硬件执行任务解耦，不将 SDK、评测和目标 GPU 环境强绑定。

## 7. 验收标准

验收是整个系统最重要的部分。最终裁决由非 LLM Evaluator 完成。

### 7.1 正确性先于性能

正确性不通过，直接 REJECT，不进入性能比较。

至少覆盖：

- 目标 dtype，以及必要的边界 dtype。
- 典型、边界和非对齐 shape。
- contiguous、non-contiguous 和 stride。
- broadcasting、空 tensor、极值、NaN/Inf（按算子语义）。
- 与固定 PyTorch 参考实现比较。
- 明确的绝对/相对误差容限。

禁止 Agent 修改容限、删除 case、缩小测试集来让候选通过。

另外准备一组主 Agent 不可见的 holdout shapes，防止只针对公开 benchmark 特判。

### 7.2 性能必须可重复

建议：

- 固定 FlagGems、PyTorch、Triton、驱动和固件版本。
- 独占设备，记录设备型号及环境指纹。
- candidate 使用独立编译缓存，避免读到旧 Kernel。
- warmup 加多轮测量。
- candidate/baseline 交错测量，减少温度和频率漂移。
- 保存中位数、离散程度和重复实验结果。
- 根据实测噪声确定接受阈值，而不是先硬编码“提升 2% 就接受”。

接受规则可以抽象为：

```text
全部正确性通过
AND 目标 workload 提升超过噪声阈值
AND 其他重要 shape 没有超过限制的退化
AND 没有新增高风险实现问题
```

微基准优化之后，最好再验证一次真实模型或算子调用链，避免只优化权重极低的 shape。

### 7.3 Git 隔离与晋级

- Champion 始终是已验收 commit。
- 每个候选从 Champion 创建独立 worktree/branch。
- 多候选并行时不能共享工作目录。
- 接受多个候选后，需要重新验证组合结果，不能假设收益可以直接相加。
- 第一版只更新本地 Champion，不自动 push、不自动提 PR。

## 8. 项目结构建议

```text
flaggems-kernel-rsi/
├── pyproject.toml
├── configs/
│   └── target.example.yaml
├── prompts/
│   ├── planner.md
│   ├── researcher.md
│   ├── profiler.md
│   └── developer.md
├── src/flaggems_rsi/
│   ├── cli.py
│   ├── driver.py           # 唯一外层迭代入口
│   ├── state.py            # 状态机、断点恢复
│   ├── schemas.py          # Plan/Result/Verdict
│   ├── agent.py            # ClaudeSDKClient、子 Agent、Session
│   ├── tools.py            # SDK MCP 工具
│   ├── workspace.py        # Git worktree、patch、revision
│   ├── evaluator.py        # 确定性 ACCEPT/REJECT
│   ├── runners/
│   │   ├── base.py
│   │   └── local.py        # 后续增加 docker/ssh
│   └── knowledge.py        # 实验记忆与摘要
├── workloads/
│   └── example.yaml
├── tests/
└── runs/
    └── <run-id>/
        ├── state.json
        ├── history.jsonl
        ├── champion.json
        ├── iterations/
        │   └── 000001/
        │       ├── context.json
        │       ├── plan.json
        │       ├── candidate.patch
        │       ├── correctness.json
        │       ├── benchmark.json
        │       ├── verdict.json
        │       └── logs/
        └── sessions.json
```

MVP 使用 JSON/JSONL 即可；需要复杂查询时再增加 SQLite。

### 8.1 建议 CLI

```bash
python -m flaggems_rsi.cli preflight --config configs/target.yaml
python -m flaggems_rsi.cli baseline --config configs/target.yaml
python -m flaggems_rsi.cli run --config configs/target.yaml --max-iterations 5
python -m flaggems_rsi.cli resume runs/<run-id>
```

## 9. 安全、隔离与预算

SDK 配置建议：

- 主模型先使用 `claude-opus-5`，子 Agent 初期继承模型，跑通后再根据实验质量决定是否分档。
- 使用 `permission_mode="dontAsk"`，未明确批准的操作直接拒绝。
- 主 Agent 只开放 Read/Grep/Glob/Agent 和必要 MCP 工具。
- 设置 `setting_sources=[]`、`strict_mcp_config=True`，避免意外加载个人配置和外部 MCP。
- 设置 `max_turns`、`max_budget_usd`、单任务超时和最大迭代数。
- 限制子 Agent 深度和并发数。
- 使用 `PreToolUse` hook 审计所有工具调用；`can_use_tool` 不会覆盖已经自动批准的调用。
- 运行候选代码时使用低权限容器、限制网络、移除不必要凭据，并设置进程组超时清理。
- 自动 push、发布、修改 Driver 和评测规则均应人工确认。

需要区分：

- `tools`：决定 Agent 实际可以使用哪些工具。
- `allowed_tools`：自动批准指定工具，不限制其他工具出现。
- `permission_mode`：控制未批准操作的后续处理方式。
- `PreToolUse` hook：审计包括自动批准在内的工具调用。
- 容器与 Runner：执行由模型生成的不可信候选代码时的实际隔离边界。

## 10. 实施路线

### Phase 0：明确优化目标

确定：

1. 目标芯片和软件栈。
2. FlagGems 固定版本。
3. 优先算子或模型 workload。
4. 优化指标：延迟、吞吐、显存，还是综合目标。
5. 每轮预算和总预算。

### Phase 1：先实现无 Agent 的评测闭环

完成：

- baseline。
- worktree。
- 正确性。
- benchmark。
- verdict。
- 实验落盘和恢复。

用一个已知慢 patch 验证会被拒绝，用一个已知正确优化验证能够接受。评测闭环没有稳定之前，不接 Agent。

### Phase 2：单候选闭环

只选择一个小算子打通：

```text
规划 → 实现 → 测试 → 基准 → 拒绝/接受
```

先跑 5～10 轮，每轮一个候选，人工检查 verdict。

### Phase 3：加入主 Session 和专业子 Agent

加入：

- 结构化计划。
- researcher/profiler/developer。
- Session resume。
- 失败记忆。
- 自动预算控制。

### Phase 4：并行搜索与组合优化

- 一轮提出 2～3 个假设。
- 使用独立 worktree。
- GPU 任务排队。
- 成功 patch 重新组合、复测。
- 按真实 workload 的收益和实验成本排序。

### Phase 5：再考虑“元 RSI”

也就是让系统优化自己的搜索策略、提示词和候选生成方式。但它仍然应该经过独立验证集和人工审批，不能允许系统直接修改自己的验收器。

## 11. MVP 验收标准

第一版至少通过这些测试：

- 错误 Kernel 即使更快也一定被拒绝。
- 无变化或变慢的 patch 不会成为 Champion。
- benchmark 超时不会卡死整个 Driver。
- 重启后能从最后一个完整状态恢复。
- Agent 无法修改 worktree 之外的代码和评测文件。
- 每个 verdict 都能追溯到源码 revision、环境、日志和原始测量。
- 重复 baseline 的噪声已经被测量，接受阈值有依据。
- 连续失败、预算耗尽、设备不可用都能正常停止。

## 12. 推荐的第一个交付版本

建议第一版实现：

```text
单算子 + 单候选 + 5 轮迭代 + 人工确认晋级
```

这条链路稳定后，再增加并行搜索、长期记忆和自动晋级。

实现前需要确定三个信息：

1. 目标芯片和后端。
2. FlagGems 仓库位置或固定版本。
3. GPU 任务在本机还是远程 Linux 机器执行。

## 13. 关键 SDK 接口

以下接口与 `claude-agent-sdk==0.2.183` 的设计相匹配：

| 需求 | SDK 接口 |
|---|---|
| 主 Session | `ClaudeSDKClient` |
| 一次性调用 | `query()` |
| 子 Agent | `ClaudeAgentOptions.agents` + `AgentDefinition` |
| 结构化规划 | `output_format={"type": "json_schema", "schema": ...}` |
| 恢复 Session | `resume=session_id` |
| 成本限制 | `max_budget_usd` |
| 轮数限制 | `max_turns` |
| 自定义领域工具 | `@tool` + `create_sdk_mcp_server()` |
| 审计工具调用 | `PreToolUse` hook |

## 14. 参考文档

- [Claude Agent SDK 概览](https://code.claude.com/docs/en/agent-sdk)
- [Python SDK 参考](https://code.claude.com/docs/en/agent-sdk/python.md)
- [子 Agent](https://code.claude.com/docs/en/agent-sdk/subagents.md)
- [Session 管理](https://code.claude.com/docs/en/agent-sdk/sessions.md)
- [结构化输出](https://code.claude.com/docs/en/agent-sdk/structured-outputs.md)
- [MCP 工具](https://code.claude.com/docs/en/agent-sdk/mcp.md)
- [FlagGems 项目](https://github.com/FlagOpen/FlagGems)

## 15. 迭代地图：系统的核心数据模型

“迭代地图”不是事后绘制的可视化面板，而是 Driver 维护的实验事实图谱。Champion 只是地图上的一个指针，真正的系统资产是完整的、带实验依据的优化谱系。

```text
算子
 └─ Baseline
     ├─ 减少内存访问
     │   ├─ 调整 Block Size → ACCEPT
     │   │   └─ 合并访存 → ACCEPT（当前 Champion）
     │   └─ 激进向量化 → REJECT（精度失败）
     ├─ 修改调度策略 → REJECT（性能退化）
     └─ 多 Stream 方案 → PARKED（受资源限制）
```

### 15.1 Operator、Lineage、IterationNode 和 Evaluation

```text
Operator（算子）
   └─ Lineage（优化谱系）
       └─ IterationNode（实验节点）
            └─ Evaluation（一次或多次评测）
```

- Operator 汇总当前 Champion、各优化分支、不同 workload 的最优版本、已否定方向、未验证组合和待消融项。
- 每个候选从任意已保存节点派生，而非只能从 Champion 派生：`parent_revision + candidate_patch → candidate_revision`。
- 一个候选对应一个不可变节点；后续评测、解释和状态变化通过附加记录关联，不覆盖原始事实。
- 每个节点记录父节点、基准 revision、patch ID、分支、代数、假设、语义变化、workload、评测和绝对创建时间。

### 15.2 事实、解释和展示分离

| 层次 | 内容 | 实验事实边界 |
|---|---|---|
| Experiment Ledger | revision、patch、环境、命令、测试、原始性能数据 | 不可变、可追溯 |
| Research Memory | Agent 的假设、失败归因、后续建议 | 解释，不能覆盖实验事实 |
| Iteration Map | 节点、边、分支、Champion、查询和视图 | 由前两者生成 |

Agent 可以更新研究记忆，但不能自行把节点改为 ACCEPTED，也不能修改原始实验记录。状态由 Driver 通过事件日志推进：

```text
NodeCreated → PatchCaptured → CorrectnessPassed
→ BenchmarkCompleted → Accepted / Rejected / Parked
```

### 15.3 用 DAG 表达父子、组合和消融

边类型包括：

- `DERIVED_FROM`：从哪个版本派生。
- `EVALUATED_ON`：在哪个 workload/环境上评测。
- `REJECTED_BY`：因哪次验证失败被拒绝。
- `COMBINES`：组合了哪些成功节点。
- `REIMPLEMENTS`：换一种实现表达同一优化思想。
- `SUPERSEDES`：在指定目标下替代哪个版本。
- `ABLATES`：为消融移除了哪个语义变化。

普通候选只有一个父节点，组合优化可有多个父节点，因此底层使用有向无环图，UI 默认按树展示：

```text
A → B ─┐
       ├→ D（组合后重新完整验证）
A → C ─┘
```

### 15.4 节点状态和失败分类

节点状态：`PLANNED / IMPLEMENTING / EVALUATING / ACCEPTED / REJECTED / PARKED / INVALID / NEEDS_REVALIDATION`。

- REJECTED 表示有明确的正确性或性能证据。
- PARKED 表示暂缓，不代表方向无效。
- INVALID 表示环境或执行过程不可信，不用于性能结论。
- ACCEPTED 只针对特定 workload、环境和基线，不代表全局最优。

分支状态：`ACTIVE / CHAMPION / MERGED / EXHAUSTED / ABANDONED / NEEDS_REVALIDATION`。

失败记忆区分：

- 思想失败：假设本身被实验否定。
- 实现失败：当前 patch 失败，但思想尚未被否定。
- 环境失败：数据不可用于判断。
- 收益不显著：正确但未超过噪声阈值。

Champion、设备或协议改变后，不删除旧节点；标记为 NEEDS_REVALIDATION 并重新入队。

### 15.5 性能结论的限定条件

性能挂在“评测条件”上，而不是只在节点保存一个 speedup。每次评测必须包含 `evaluation_id`、候选节点、明确的 baseline 节点、协议版本、环境指纹、workload、正确性、原始重复测量、中位数、噪声和 verdict。

结论由以下条件共同限定：

```text
baseline_node_id + environment_fingerprint + benchmark_protocol_version
```

这样不会把“相对旧基线的提升”误标为“相对当前 Champion 仍有提升”。

## 16. 语义变化与消融实验

一个 patch 同时合并访存、修改 Block Size、调整 num_warps 时，即使整体变快，也无法直接归因。开发 Agent 的计划必须列出 `semantic_changes`，每项包含：

- `change_id`、类型、描述、文件、预期效果。
- 消融策略：`INDEPENDENT / DEPENDENT / INTERACTING / NOT_ISOLABLE`。
- 依赖关系和可选的、由可信 fixture 解释的显式操作，不包含 shell 命令。

消融示例：

```text
Baseline A
   ↓
完整候选 D = C1 + C2 + C3
   ├─ D_without_C1
   ├─ D_without_C2
   └─ D_without_C3
```

消融必须遵守：

1. 同环境、同 workload、同评测协议，与完整候选交错测量。
2. 从共同父版本重新构造“只缺少目标变化”的实现，不将反向 Git diff 直接当成语义消融。
3. 对无法自动拆分的 patch，标记 NOT_ISOLABLE，先要求拆成更小的语义提交。
4. 消融实现仍须通过相同正确性测试，结果由同一个确定性 Evaluator 裁决。

## 17. 地图驱动的研究规划

每轮向主 Session 提供压缩后的地图，而不是所有原始日志：

```json
{
  "operator": "softmax",
  "champion_node": "softmax_0007",
  "best_path": ["baseline", "softmax_0002", "softmax_0005", "softmax_0007"],
  "active_branches": [],
  "failed_directions": [],
  "untested_combinations": [],
  "pending_ablations": [],
  "coverage_gaps": [],
  "revalidation_required": []
}
```

规划规则：

1. Champion 仍有明确瓶颈，从 Champion 派生。
2. 历史分支有潜力但实现失败，从共同父节点换一种实现。
3. 成功分支修改区域独立，提议组合并重新完整验证。
4. 多变化候选归因不清，优先消融。
5. 连续无收益，切换分支或算子，不重复微调同一参数。
6. 覆盖不足时先补 baseline、shape 或 profile，而不是盲改。

相同代码身份使用 `base_revision + normalized_patch_sha256` 检测，避免把重复实验当成新优化。只有环境和协议仍匹配的可信结果才可复用。

## 18. 地图的存储、查询与更新后的实施路线

### 18.1 存储

```text
runs/<run-id>/
├── map/
│   ├── nodes/
│   ├── evaluations/
│   ├── edges.jsonl
│   ├── events.jsonl
│   └── champions.json
├── artifacts/patches/
├── iterations/
└── champion.json
```

MVP 使用原子 JSON 和 JSONL，后续可迁移 SQLite。节点 ID 便于阅读，代码身份由 revision 和 patch 哈希决定。评测和事件追加保存，Agent 获得只读摘要。

### 18.2 CLI 视图

```bash
flaggems-rsi map show --run runs/<id> --operator softmax
flaggems-rsi map path --run runs/<id> --node softmax_0007
flaggems-rsi map diff --run runs/<id> --left softmax_0005 --right softmax_0007
flaggems-rsi ablation plan --run runs/<id> --node softmax_0007
flaggems-rsi ablation run --run runs/<id> --node softmax_0007
```

### 18.3 实施顺序

- Phase 1：评测闭环与不可变地图同步实现，不能等 Agent 跑通后补地图。
- Phase 2：支持任意历史节点分支、失败分类、patch 去重和压缩地图规划。
- Phase 3：实现语义变化、单变量消融和归因驱动的保留/回滚/组合。
- Phase 4：支持多父组合、跨分支全量复测和多 workload Champion。
- Phase 5：支持地图摘要、覆盖空洞、协议升级批量复验和搜索策略调整；仍禁止自动修改验收规则。

### 18.4 地图新增验收项

- 每个候选能追溯完整父链和精确 patch，失败节点不被覆盖。
- 可从任意历史节点派生，Champion 路径可重建并解释晋级依据。
- 多父节点显示全部来源，同代码/环境不生成难以区分的“新优化”。
- 消融只移除目标语义变化，并通过同一正确性测试。
- 环境失败、实现失败、思想失败和收益不显著分开记录。
- 更换基线后，不沿用旧提升数字；地图只读视图不能修改实验事实。

## 19. 本次可运行 MVP 的落地范围

用户已确认第一阶段采用本地模拟评测，交付可运行 MVP。使用独立的临时 Git Python fixture 项目验证真实的 patch 捕获、独立 worktree、测试、基准、谱系和消融流程，不声称获得真实 GPU 性能提升。

实现模块：

- `schemas.py`：Pydantic 配置、计划、节点、边、评测和任务协议。
- `storage.py` / `map.py` / `knowledge.py`：不可变存储、DAG、Champion 和研究摘要。
- `workspace.py`：Git patch、路径限制、历史节点重放和独立 worktree。
- `runners/`：结构化 JobSpec、本地固定 argv Runner 和模拟 fixture Runner。
- `evaluator.py`：正确性优先、噪声估计、目标收益和保护 workload 回归检查。
- `ablation.py`：只针对可信 fixture 显式操作构造消融；不能构造时明确拒绝。
- `agent.py` / `tools.py`：主 Session、子 Agent、结构化输出、只读 MCP 和权限审计。
- `driver.py` / `cli.py`：单候选状态机、恢复、地图与消融命令。

默认不自动 commit/push/PR，不修改本机 FlagGems 仓库，不开放任意远程 shell。真实模型调用只在用户显式运行 Agent 迭代时发生；测试通过 fake agent 和本地 fixture 完成，不产生 API 费用。

当前不是完整硬件版本：真实 GPU/NPU Runner、真实 Triton profiling、任意 Kernel patch 的自动语义拆分、并行搜索和元 RSI 留待后续接入。

## 20. FlagGems-vllm 与跨厂商性能证据

目标测试仓库为 `git@github.com:tonyh168/FlagGems-vllm.git`，使用其中面向 vLLM 的融合 Triton 算子和公开测试/benchmark。小算子优先选择 `moe_align_block_size`，先单卡、短 warmup/iter，再逐步进入真实 profiling 和单候选迭代。

目标厂商为 NVIDIA、沐曦、海光、昇腾、平头哥。具体主机通过用户已配置的 SSH 别名访问，只读巡检后选择空闲卡，不把主机凭据或任意 shell 暴露给 Agent。

```text
设备巡检 → 固定源码/依赖 → correctness/holdout
→ baseline timing → 真实 profiler → 原始证据与摘要
→ 性能下界 → 单变量优化/消融 → 地图节点
```

`docs/hardware-profiling.md` 记录厂商工具矩阵、实际探测状态和限制；`.claude/skills/kernel-profiling/SKILL.md` 将这套流程固化为可复用工作手册。关键区分：SMI 只监控设备，pytest benchmark 只做计时，Profiler 才能提供 Kernel 内部瓶颈证据。

## 21. 榨干硬件：理论极限与效率目标

最终目标不是单纯超过 vLLM baseline，而是对每个设备、shape/dtype/layout 和算法实现建立可解释的性能下界：

```text
T_memory = required_bytes / effective_bandwidth
T_compute = required_flops / effective_compute_rate
T_roofline = max(T_memory, T_compute)   # 只有资源充分重叠时成立
T_lower_bound = T_roofline + launch + synchronization
Efficiency = T_lower_bound / measured_kernel_time
```

有效带宽和计算速率必须通过相同硬件与访问模式的微基准标定，不能直接使用厂商标称峰值。launch、dispatch、同步和算法依赖不能被隐藏在 roofline 里。

对于 MoE、attention、排序、量化和多阶段同步算子，必须分阶段计算算法依赖、实际流量、计算和同步下界，不能用简单 bytes/FLOPs 宣称“100% 理论极限”。详细规则见 `docs/performance-model.md`，可执行计算见 `src/flaggems_rsi/performance.py`。

地图节点新增关联：`device_profile_id`、`roofline_id`、`profiler_artifact_ids` 和 `bottleneck_class`。优化历史因此能表达“从 launch 瓶颈变为 memory 瓶颈，再变为 compute 瓶颈”，而不只是一个 speedup 数字。跨厂商比较各自相对可验证下界的效率，不以绝对耗时直接判断芯片优劣。

当效率大于 1 时，必须优先检查漏算流量/FLOPs、异步未同步、计时错误、缓存或旧 Kernel 复用、错误的重叠假设，而不是记录“超过物理极限”。
