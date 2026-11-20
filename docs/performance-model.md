# 从“超过 Baseline”到“逼近理论极限”

## 目标定义

Speedup 只说明相对某个 vLLM 实现更快，不能说明硬件已经被榨干。最终目标是：在正确性、公开/私有 workload、指定设备与协议下，逼近一个**可解释的性能下界**，并将瓶颈从一种资源转移到另一种资源的过程记录到迭代地图。

```text
baseline latency → profile → 性能下界 → 瓶颈假设 → 单语义优化
→ 同条件复测 → 新瓶颈 → 消融归因 → Champion/分支演进
```

## 每个 workload 的输入

- shape、dtype、layout、stride、batch/sequence、sparsity 或 expert routing。
- 必需输入/输出/中间读写 bytes，区分 DRAM、L2、shared/local memory 和寄存器。
- 必需 FLOPs/整数操作、指数/除法/排序等特殊指令与同步次数。
- 实际 kernel launch、grid/block/warp、occupancy、流水线与内存访问证据。
- 标称峰值、通过厂商微基准测得的有效带宽/有效计算速率。
- launch、Python/dispatch、synchronization 等不能被 Kernel 内部优化消除的成本。

## 基础 roofline

```text
T_memory  = required_bytes / effective_bandwidth
T_compute = required_flops / effective_compute_rate
T_roofline = max(T_memory, T_compute)       # 仅当资源可充分重叠时
T_lower_bound = T_roofline + T_launch + T_sync
Efficiency = T_lower_bound / measured_kernel_time
```

若访存和计算不能重叠，使用 `T_memory + T_compute`，不要套用 `max`。使用 `performance.PerformanceModel` 时，必须显式填写 `overlap_assumed` 和 `assumptions`。

标称峰值只能做乐观参考。有效带宽与有效 FLOP/s 应在相同设备、精度、频率和访问模式下用独立微基准标定，并写入 `DeviceProfile`。测量必须区分 kernel、operator 和 wrapper 时间，不把 host overhead 隐藏到 Kernel roofline 里。

## 不同算子的性能下界

### Memory-bound

例如拷贝、简单归一化、量化/反量化：

```text
bytes = input_read + output_write + necessary_intermediate_traffic
T ≥ bytes / effective_bandwidth
```

优化重点：减少重复读写、提高合并访存、减少不必要的格式转换、改善 occupancy 与访问粒度。接近带宽极限后，继续调 launch 配置通常不会带来成比例收益。

### Compute-bound

例如充分复用数据的矩阵乘和部分融合计算：

```text
T ≥ required_flops / effective_compute_rate_for_dtype_and_instruction
```

优化重点：数据复用、流水线、tile、寄存器/shared memory 压力、特殊函数与 tensor core 利用率。不能把 CUDA core 峰值当作 tensor core 或超越函数峰值。

### Launch / overhead-bound

小 shape 或大量小 Kernel：

```text
T_measured ≈ T_launch + T_kernel + T_dispatch + T_sync
```

优先减少 launch 数、同步、分配、Python/dispatch 开销或做安全融合。小 Kernel 的 DRAM roofline 可能很小，但实际耗时远大于该下界。

### 复杂 MoE / attention / sorting

这些算子受多阶段依赖、负载不均、稀疏路由、原子操作、shared memory、同步与排序算法约束。不能简单将输入/输出 bytes 当成总访存量，也不能用一个 FLOP 峰值代表整个 Kernel。

应拆成阶段并建立：

```text
T_stage_lower_bound = max(algorithmic_dependency_bound,
                          memory_traffic_bound,
                          compute_bound,
                          synchronization_bound)
```

记录 aligned/sorted 元素、读写放大、radix/扫描/比较操作、尾块和负载不均。只有模型假设与 profiler 证据一致时，才可宣称接近该下界。

## 厂商特定模型

- NVIDIA：结合 Proton/Nsight Compute 的 DRAM/L2/shared 吞吐、warp stalls、occupancy、tensor core 与特殊指令指标。
- MetaX：结合 AP cycle、GVM/shared memory 延迟、指令宽度、Partial Read/Write 与 Perf Counter。
- Hygon：按 DTK 实际计数器建立 HIP/Kernel 模型，AMD 指标不能直接宣称兼容。
- Ascend：按 MTE1/MTE2/MTE3、Cube、Vector、Scalar pipeline 分别计算理论时间与利用率，避免用单一 TOPS 替代流水线瓶颈。
- PPU：用 Asight Compute 的多级内存/指令 roofline 和源码热点；在工具尚未验证前只记录设备信息，不虚构效率。

## 地图中的记录

每次迭代关联：

- `device_profile_id`：设备、驱动、编译器、软件栈和有效吞吐标定。
- `roofline_id`：workload 输入、计算/访存假设、下界公式版本。
- `profiler_artifact_ids`：原始采集文件、工具版本、采集参数与验证状态。
- `bottleneck_class`：launch/memory/compute/occupancy/synchronization/unknown。
- `efficiency` 与其分子分母：不能只保存一个百分比。
- 相同环境、协议、baseline 和 workload 的复测与消融结果。

当瓶颈从 memory 变为 compute、从 compute 变为 launch，说明优化进入另一阶段，而非一定失败。若效率超过 1，首先检查漏算流量/FLOPs、错误计时、异步未同步、编译缓存复用、模型假设错误或读到了旧 Kernel。

## 验收原则

1. 先达到正确性，再超过 baseline，最后逼近性能下界。
2. 达到下界附近时，用独立 profiler 和消融证明瓶颈，而不是依赖模型自我评价。
3. 对所有关键 shape 检查 guard 回归，防止只为一个点特化。
4. 不同厂商只比较相对各自可验证下界的效率，不直接将绝对耗时当成跨厂商优劣。
5. 当模型误差已接近测量噪声时，停止盲调并补 profiling 或切换分支。
