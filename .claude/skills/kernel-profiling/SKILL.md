---
name: kernel-profiling
description: 跨 NVIDIA、沐曦、海光、昇腾、平头哥的 FlagGems-vllm/Triton Kernel 性能分析、真实工具探测、证据采集和理论极限归因。用于选择 profiler、解读硬件指标、建立 roofline、分析 kernel 瓶颈或将性能证据关联到迭代地图。
---

# 跨厂商 Kernel Profiling

## 必须区分

1. `nvidia-smi` / `mx-smi` / `hy-smi` / `npu-smi` / `ppu-smi` 是设备巡检，不是 Kernel 内部 profiler。
2. pytest benchmark 是计时，不自动证明瓶颈原因。
3. profiler 原始数据、工具版本、参数、设备、Kernel、shape/dtype/layout、source revision 必须一起保存。
4. 没有成功采集/验证的数据标为 `unverified`，不能编造 counters 或宣称达到理论极限。
5. 模型只读摘要和受控 artifact；不能通过任意远程 shell 执行 profiling。

## 标准流程

```text
只读巡检 → 选空闲单卡 → 环境指纹 → correctness/holdout
→ 短 baseline → 真实 profiler → 原始 artifact → 摘要
→ roofline/算法下界 → 单语义假设 → 复测/消融 → 地图节点
```

先阅读 `docs/hardware-profiling.md` 的厂商具体命令和已验证环境，阅读 `docs/performance-model.md` 建立下界。参考 `src/flaggems_rsi/performance.py` 的 `PerformanceModel` 计算并记录假设。

## 工具选择

| 目标 | NVIDIA | MetaX | Hygon | Ascend | PPU |
|---|---|---|---|---|---|
| 资源 | nvidia-smi | mx-smi | hy-smi | npu-smi | ppu-smi / DCGM |
| Timeline | Proton / nsys | mcTracer | 按 DTK 实测 | msprof op / MindStudio | Asight Systems |
| Kernel 内部 | ncu / Proton | Perf Counter / Cycle Trace | 按 DTK 实测 | msprof simulator / Insight | Asight Compute |

## 性能分析

- 先计算 `T_memory`、`T_compute`、launch/sync 和算法依赖下界。
- 标称峰值不等于有效吞吐；先标定相同设备/精度/访问模式下的有效带宽与计算速率。
- `efficiency = lower_bound / measured_time`；大于 1 时优先排查漏算流量、异步未同步、计时错误、缓存/旧 Kernel 或错误假设。
- 小 Kernel 重点看 launch 和 dispatch；大 Kernel 关注 DRAM/L2/shared、occupancy、流水、warp stalls、特殊指令和同步。
- MoE/attention/sort 必须分阶段建模，不能把所有操作简化为一个 bytes/FLOPs 比值。
- 比较必须限定同一设备、同一协议、相同 shape/dtype/layout 和 baseline；不直接用绝对耗时做跨厂商优劣结论。

## 远程安全

- 先只读检查进程和空闲卡，每卡一个性能任务，短 warmup/iter。
- 不自动 reset、锁频、杀进程、推送代码或分发多机任务。
- 不擅自改变已有环境，依赖放隔离 venv；缺工具时记录实际错误并停止该 profiler 路径。
- Ascend 仿真只支持单卡/卡 0；PPU 命令未实测前不可套用其他厂商语法。

## 结果交付

报告：设备与软件版本、source SHA、测试/计时命令、是否通过正确性、原始日志/结果路径、profiler 是否真实成功、性能下界及假设、瓶颈和下一轮单变量优化建议。将 `device_profile_id`、`roofline_id`、`profiler_artifact_ids`、`bottleneck_class` 写入迭代地图。
