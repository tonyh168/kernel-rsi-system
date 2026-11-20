# 当前硬件能力与接入状态

2026-11-17 更新。

| 能力 | 状态 | 说明 |
|---|---|---|
| FlagGems-vllm NVIDIA 小算子正确性 | 已验证 | `nv-h20` 卡 2，`moe_align_block_size --quick`，128 passed |
| NVIDIA vLLM 对比 benchmark | 环境补齐中 | vLLM 缺失导致首次跳过；固定 wheel 在隔离 venv 安装，尚无有效时延结果 |
| NVIDIA 真实 profiler | 工具已发现，未采集 | Proton/NCU 路径已确认，不能将 binary 存在当作已验证 trace |
| MetaX | 工具已发现 | metax-57：MACA 3.7.1 mcTracer、mx-smi |
| Hygon | 管理工具已发现 | hygon-19：hy-smi、DTK；profiler 尚待验证 |
| Ascend | profiler 已发现 | ascend-116：CANN 8.5.0 msprof、npu-smi；未运行仿真 |
| PPU | 官方资料已整理，工具未实测 | ppu-1 网关不可达；Asight 具体命令待可达机器验证 |
| 自动远程 Runner | 未实现 | 本轮真实试跑通过审核后的固定 SSH 命令，未接 Agent 自动执行 |
| 理论极限模型 | 已有实现与测试 | `PerformanceModel`；实际有效带宽/FLOPs 仍需硬件标定 |

详细证据见 `docs/nvidia-h20-smoke-test.md`、`docs/hardware-profiling.md` 和 `artifacts/nvidia-h20-moe-align/`。
