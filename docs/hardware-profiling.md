# 跨厂商 Kernel Profiling 工作手册

验证日期：2026-11-17。当前手册将**设备监控**、**端到端计时**、**真实 profiler 采集**分开，任何工具不存在或采集失败都必须明确记录，不能将 SMI 输出当成 Kernel profiling。

## 统一证据链

```text
只读资源巡检
  → 设备/驱动/编译器/依赖指纹
  → 正确性与公开/私有 holdout
  → 固定 shape/dtype/layout 的 baseline
  → warmup + 交错重复 kernel timing
  → 受限次数的真实 profiler
  → 原始 trace + 工具/版本 + 解析摘要
  → roofline/算法下界与瓶颈归因
  → 将证据关联到迭代地图节点
```

- 一张卡同一时刻只运行一个 benchmarking/profiling 任务；不抢占已有任务。
- 不自动 reset、锁频、杀进程、跨机器并行测试或改变其他任务的环境。
- profile 原始文件不可变保存，模型只能读取摘要和受控路径。
- 无硬件计数器、无法解析或无法验证的结果标为 `unverified`，不能给 Agent 当成真实测量。
- 微基准结合 vLLM model-profile 中 prefill/decode 的占比与调用次数，避免优化无实际权重的 shape。

## 厂商工具矩阵

| 厂商 | 设备巡检 | Kernel/系统分析工具 | 已验证环境与注意事项 |
|---|---|---|---|
| NVIDIA | `nvidia-smi` | Triton Proton；Nsight Compute `ncu`；Nsight Systems `nsys` | `nv-h20`：H20-3e，`/usr/local/bin/proton`、`/usr/local/cuda/bin/ncu` 已确认；`nsys` 默认 PATH 尚未确认 |
| 沐曦 MetaX | `mx-smi` | `$MACA_PATH/bin/mcTracer`；Compiler Explorer；Perf Counter；Cycle Trace | `metax-57`：`/opt/maca-3.7.1/bin/mcTracer` 已确认。采集选项必须按本机版本 `mcTracer --help` 验证 |
| 海光 Hygon | `/opt/hyhal/bin/hy-smi` | DTK 自带 profiler，可能为 `rocprof`/`rocprofv3`/`hipprof` | `hygon-19` 已确认 hy-smi 和 `/opt/dtk`，尚未确认可用 profiler；不可直接照搬 AMD 命令 |
| 昇腾 Ascend | `npu-smi info` | `msprof op`、`msprof op simulator`、MindStudio Insight | `ascend-116`：`/usr/local/Ascend/cann-8.5.0/bin/msprof` 已确认；仿真仅单卡且仅卡 0，不能在不空闲卡 0 上擅自执行 |
| 平头哥 PPU | `ppu-smi`、PPU DCGM | Asight Compute（Kernel 计数器、roofline、源码热点）；Asight Systems（系统 timeline） | `ppu-1` 网关不可达，命令尚未实测；先查 SDK 文档与机器内 `asys`/Asight 安装，不能套用其他厂商命令 |

## NVIDIA

### Preflight 与轻量采集

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
  --format=csv
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv
/usr/local/cuda/bin/ncu --version
proton --version
```

`nvidia-smi` 只用于资源、利用率、显存和进程归属，不提供具体 Kernel 的指令/访存瓶颈。

### Triton Proton

先查看本机 `proton --help`，以单算子、单 shape、单卡和限定 warmup/iter 采集，不使用全模型长任务。官方支持 `proton [options] pytest [pytest_args]` 形式。保存原始 profile、控制台日志、pytest 参数和 kernel 映射。解析后再向 Agent 返回耗时热点与趋势，不能把全程序 timeline 当成单次 Kernel latency。

### Nsight Compute

`ncu` 适合详细硬件计数器、memory workload、occupancy、warp stalls 和指令分析。采集范围必须限定为正确性已通过的微基准，必要时用 `--target-processes all` 跟踪 Python 子进程；其他选项以本机 `ncu --help` 为准。

```bash
/usr/local/cuda/bin/ncu --target-processes all \
  <本机支持的 kernel/launch 限制选项> \
  <固定评测入口及参数>
```

先进行小采集，不直接对全量 pytest 或长服务开启全指标 profiling。保存 `.ncu-rep` 和文本导出。`nsys` 用于 CPU/GPU timeline、launch、同步和流空隙，不能替代 `ncu` 的 Kernel 内部指标。

## 沐曦 MetaX

`mcTracer` 在 MACA 安装目录下，基本调用形式为：

```bash
mcTracer [tracer parameters] [user program] [user program parameters]
```

已确认 `metax-57` 使用 MACA 3.7.1；实际 tracer 参数需先读取本机帮助。MACA 调优指南还建议：

- Compiler Explorer：检查汇编、occupancy、private memory 和内存指令宽度。
- AP clock counter：对可疑代码段分段计时。
- Perf Counter：结合性能模型定位硬件瓶颈。
- Cycle Trace：检查 AP/cycle 指令发射、Partial Read/Write、shared-memory 与 GVM 长延迟操作。

这些证据需映射到 Kernel 名、编译配置和输入 shape，不以程序总耗时代替具体瓶颈。

## 海光 Hygon

DTK 与 ROCm 有生态联系，但实际二进制和计数器支持必须以目标 DTK 为准。流程：

```bash
/opt/hyhal/bin/hy-smi
command -v rocprof rocprofv3 hipprof || true
find /opt/dtk -maxdepth 4 -type f -name 'rocprof*' -o -name 'hipprof*'
```

如果工具存在，先阅读版本化帮助，再选择 HIP trace、kernel trace、stats 或 counters。ROCm 文档只能作为语义参考，不能声称 Hygon 支持全部 ROCm PC sampling/ATT 功能。若仅有 `hy-smi`，则只能做资源巡检，无法进行 Kernel 内部归因。

## 昇腾 Ascend

实卡：

```bash
msprof op --kernel-name=<target_kernel> --output=<private-output-dir> \
  python3 <fixed-test.py>
```

指令级仿真：

```bash
export LD_LIBRARY_PATH=<CANN>/ascend_toolkit/latest/tools/simulator/<soc>/lib:$LD_LIBRARY_PATH
export TRITON_DISABLE_LINE_INFO=0
msprof op simulator --kernel-name=<target_kernel> \
  --soc-version=<由 npu-smi 确认的具体型号> python3 <fixed-test.py>
```

关注 `op_summary_*.csv`、`trace.json`、`visualize_data.bin` 与 MTE1/MTE2/MTE3、Cube、Vector、Scalar pipeline。仿真仅支持单卡/卡 0，且部分 Triton 算子采集功能有限制；必须在许可证、CANN 版本、SoC 型号匹配时执行。

## 平头哥 PPU

PPU-SMI 负责设备、拓扑、利用率、错误和资源归属；DCGM 负责集群监控。Asight Systems 用于 CPU/PPU/API 时间线，Asight Compute 用于硬件计数器、分级 memory roofline、源码热点和指令级分析。

当前只有官方 SDK 工具名称和能力说明，没有验证过的命令行。应在可达 PPU 节点上确认 SDK 版本、固件、工具路径及帮助后再编写采集适配器。不得把 `msprof`、`rocprof`、`ncu` 参数套用到 PPU。

## 参考资料

- [FlagGems-vllm 测试指南](https://github.com/tonyh168/FlagGems-vllm/blob/main/docs/content/en/testing/unittest.md)
- [FlagGems-vllm 基准指南](https://github.com/tonyh168/FlagGems-vllm/blob/main/docs/content/en/performance/benchmark.md)
- [NVIDIA Nsight Compute](https://docs.nvidia.com/nsight-compute/)
- [Triton Proton](https://triton-lang.org/main/python-api/generated/triton.profiler.proton.html)
- [MetaX MACA Kernel 性能分析指南](https://gitee.com/metax-maca/mxmaca-performance-tuning-guide/blob/e42f3c23510964cbb85a89220e7be9dcadf5e632/guide/ch6.Kernel%E6%80%A7%E8%83%BD%E5%88%86%E6%9E%90%E5%B7%A5%E5%85%B7.md)
- [Triton-Ascend Profiling Guide](https://github.com/triton-lang/triton-ascend/blob/e599a68d47edecc831add2ea1ae9690a8e3d756e/docs/en/debug_guide/profiling.md)
- [昇腾 MindStudio Profiling](https://www.hiascend.com/en-sg/document/detail/en/mindstudio/830/TITools/Profiling/atlasprofiling_16_0005.html)
- [PPU SDK 文档](https://help.aliyun.com/zh/document_detail/3011255.html)
- [ROCm Profiler 文档（仅作 Hygon 的参考，非 DTK 兼容性证明）](https://rocm.docs.amd.com/projects/rocprofiler/en/latest/)
