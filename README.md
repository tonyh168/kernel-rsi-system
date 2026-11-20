# FlagGems Kernel RSI MVP

这是一个**实验驱动的 Kernel 自动优化控制面 MVP**。它把每次候选修改记录为不可变实验节点，以 Git patch 为代码事实，以独立 Runner 和确定性 Evaluator 决定候选是否晋级，并维护支持分支、组合与消融的迭代地图。

## 当前边界

当前交付已接通：

- 不可变节点、谱系 DAG、Champion 路径和状态迁移。
- Git worktree 隔离、patch 捕获与路径白名单。
- 正确性优先、交错重复测量、噪声阈值和保护 workload 回归检查。
- 本地 CPU fixture 的端到端优化与显式消融。
- Agent SDK 主 Session、researcher/profiler/developer 子 Agent 的实际接入代码。
- 地图查看、历史路径、节点对比、手动晋级、恢复和消融 CLI。

**当前没有运行真实 GPU/NPU 测试，也没有证明任何真实 FlagGems Kernel 性能提升。** `simulated` 模式只是一个可编辑的 Python CPU workload，用来验证控制面。真实 Triton 评测和 profiling 需要后续接入目标硬件 Runner。

## 安装

需要 Python 3.11+、Git，以及已安装的 `claude-agent-sdk>=0.2.183`。

```bash
python3 -m pip install -e '.[test]'
```

如果不安装包，可在项目根目录使用：

```bash
export PYTHONPATH="$PWD/src"
```

## 离线端到端演示

演示不调用模型 API、不产生 API 费用，也不修改 `/Users/baai/Desktop/baai_proj/FlagGems`。

### 1. 初始化 fixture 与 baseline

```bash
python3 -m flaggems_rsi.cli init \
  --config configs/local-sim.json \
  --run runs/local-sim
```

`init` 在 `examples/sim_repository` 创建一次性的 Git fixture。默认配置引用该 fixture；如果它已经存在，则要求它是干净的 fixture 仓库，Driver 不会改写它。

### 2. 执行一个离线优化迭代

```bash
python3 -m flaggems_rsi.cli run \
  --run runs/local-sim \
  --offline \
  --max-iterations 1
```

离线 Agent 把 `WORK_FACTOR = 4` 改为 `1`，Driver 捕获实际 patch，并运行正确性、公开 workload 和内部 holdout 检查。

默认即使 Evaluator 给出 `ACCEPT`，节点也进入 `PARKED`，等待人工晋级：

```bash
python3 -m flaggems_rsi.cli promote --run runs/local-sim --node sim_copy_0001
```

仅对可信模拟流程演示自动晋级时，显式指定：

```bash
python3 -m flaggems_rsi.cli run --run runs/local-sim --offline --max-iterations 1 --auto-promote
```

## 迭代地图

```bash
python3 -m flaggems_rsi.cli map show --run runs/local-sim --operator sim_copy
python3 -m flaggems_rsi.cli map path --run runs/local-sim --node sim_copy_0001
python3 -m flaggems_rsi.cli map diff --run runs/local-sim --left sim_copy_0000 --right sim_copy_0001
```

示例：

```text
sim_copy
sim_copy_0000 baseline [ACCEPTED]
  sim_copy_0001 Reduce the fixture's redundant repeated work [CHAMPION]
```

节点包含：

- 父节点、base revision、candidate patch ID 和分支代数。
- 假设、目标 workload、语义变化和消融属性。
- 评测 ID、环境指纹、协议版本、状态和失败分类。

底层是 DAG；多父组合的边关系独立于默认树形展示。组合节点必须重新进行全量评测，不能把父分支的收益直接相加。

## 消融

先查看显式消融计划，再运行：

```bash
python3 -m flaggems_rsi.cli ablation plan --run runs/local-sim --node sim_copy_0001
python3 -m flaggems_rsi.cli ablation run --run runs/local-sim --node sim_copy_0001
```

内置消融适配器只支持 CPU fixture 的 `WORK_FACTOR` 语义变化。它不会尝试从任意 Triton patch 猜测反向修改。真实 Kernel 的消融需要先声明语义变化，并提供经过审查的构造适配器；无法拆分时明确返回 `NOT_ISOLABLE`。

## 使用真实模型 Agent

以下命令会实际调用模型并可能产生费用：

```bash
export ANTHROPIC_API_KEY='...'
python3 -m flaggems_rsi.cli run --run runs/local-sim --max-iterations 1
```

主 Session 使用 `ClaudeSDKClient`，保存 planner 的 `session_id`，进程重启后可 `resume`。planner 通过 `output_format` 返回结构化 `IterationPlan`；researcher 和 profiler 只读；developer 只能在候选 worktree 内编辑白名单文件。

安全默认值：

- `permission_mode="dontAsk"`，未批准操作拒绝。
- `setting_sources=[]`、`strict_mcp_config=True`。
- 禁用 developer 的 Bash/Agent，主 Session 不能编辑文件。
- `max_turns`、`max_budget_usd`、子 Agent 深度和并发限制。
- `PreToolUse` 记录工具调用到 `agent-tools.jsonl`。
- MCP 仅暴露地图摘要和受控模拟 profile，不暴露任意 shell 或私有 holdout。

恢复离线迭代：

```bash
python3 -m flaggems_rsi.cli resume --run runs/local-sim --offline --max-iterations 1
```

## 实验目录

```text
runs/local-sim/
├── config.json
├── state.json
├── map/
│   ├── nodes/
│   ├── evaluations/
│   ├── edges.jsonl
│   ├── events.jsonl
│   └── champions.json
├── artifacts/patches/
├── workspaces/
├── jobs/
└── iterations/
```

Experiment Ledger 是事实来源；Agent 的研究解释不会覆盖原始 patch、环境或测量。相同 base revision 和 patch 哈希会被去重。协议或环境变化后，旧结果标记为需要重新验证，不能作为相对当前 Champion 的新提升。

## 测试

```bash
python3 -m pytest
```

测试覆盖地图、patch、正确性优先、噪声判定、恢复、分支/消融以及 SDK 配置。全部使用本地 fixture 或 mock，不调用真实模型和 GPU。

## 后续接入真实 FlagGems

1. 固定 FlagGems、PyTorch、Triton、驱动和固件版本。
2. 将 `local` Runner 的固定 argv 替换为经过审查的真实评测适配器，返回相同的结果协议。
3. 加入独占设备的 Docker/SSH Runner、设备资源队列和原始硬件 profile。
4. 根据真实重复 baseline 测量噪声调整阈值，保留公开测试和私有 holdout 的隔离。
5. 先运行单算子、单候选、人工晋级，再启用多分支组合和长期自动迭代。

完整设计见 `FlagGems-Kernel-RSI-系统设计文档.md`。

## FlagGems-vllm 与跨厂商真实硬件

目标仓库为 `git@github.com:tonyh168/FlagGems-vllm.git`，面向 vLLM 的融合 Triton 算子，而不是把通用 FlagGems 的测试命令直接套用过来。

- 设计文档：`docs/hardware-profiling.md`。
- 理论极限模型：`docs/performance-model.md` 与 `src/flaggems_rsi/performance.py`。
- 可复用项目 Skill：`.claude/skills/kernel-profiling/SKILL.md`。
- **跨厂商镜像与组件登记：`docs/test-environments.md`**，后续 sparse MLA 测试以此为环境来源。
- **Issue #1324 调查与防御性修复：`docs/issue-1324-sparse-mla.md`**，patch 与原始日志在 `examples/issue1324/`、`artifacts/issue1324/`。
- NVIDIA 小算子配置：`configs/nvidia-h20-moe-align.json`。
- 真实测试 artifact：`artifacts/nvidia-h20-moe-align/`。

厂商包括 NVIDIA、沐曦、海光、昇腾、平头哥。设备监控、计时和真实 profiler 严格分开；工具仅在版本和实际采集验证后标为可用。

最终目标从 `baseline_latency / candidate_latency` 扩展到：在每个 workload 的可解释访存、计算、launch、同步及算法依赖下界上，测量 `efficiency = lower_bound / measured_time`。不把超过 baseline 等同于榨干硬件，也不把标称峰值当成有效吞吐。

真实远程执行采用 `inspect → baseline → profile → bounded benchmark`，一卡一个性能任务，不自动锁频、reset、杀进程或跨机器分发。当前实现已加入设备 profile、profiling artifact 和 roofline 数据模型，但尚未完成通用 SSH Runner；硬件试跑采用经过审查的固定命令，不让 Agent 执行任意远程 shell。
