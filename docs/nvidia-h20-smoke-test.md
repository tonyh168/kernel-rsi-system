# NVIDIA H20 真实小算子试跑

日期：2026-11-17。

## 配置

- 仓库：`git@github.com:tonyh168/FlagGems-vllm.git`
- 固定 revision：`af184e5731c814883659f2e855209e14c349383d`
- 主机：`nv-h20`
- 设备：`CUDA_VISIBLE_DEVICES=2`，NVIDIA H20-3e，143771 MiB，驱动 580.95.05。
- Python 3.12.12、PyTorch 2.9.0+cu128、Triton 3.5.0、FlagGems 5.4.0。
- 工具：`/usr/local/cuda/bin/ncu` 2025.1.1、`/usr/local/bin/proton`。
- 隔离依赖：`/root/flaggems-vllm/.venv`，使用 `--system-site-packages`，没有替换基础 torch/Triton 环境。

设备巡检发现卡 0/1 已有 vLLM 任务，因此选择空闲卡 2。未杀进程、未 reset、未锁频。

## 已验证正确性

```bash
cd /root/flaggems-vllm
CUDA_VISIBLE_DEVICES=2 PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_moe_align_block_size.py --quick \
  --record json --output artifacts/correctness.json
```

实际结果：**128 passed, 1 warning in 30.42s**。

此 revision 的 `--record` 需要参数 `json`，与在线文档中无参数的 `--record` 示例不同。实际命令以仓库 conftest 和 `pytest --help` 为准。

## Benchmark 状态

`moe_align_block_size_triton` 的 benchmark 依赖 `vllm._custom_ops.moe_align_block_size`。初始环境未安装 vLLM，pytest 返回 `1 skipped`，不是性能测试成功。

为补齐 vLLM baseline，已在隔离 venv 中按仓库固定链接安装 vLLM 0.24.0，使用 `--no-deps` 避免覆盖厂商 torch/CUDA 环境。安装完成后需重新运行 benchmark；若依赖不完整，将记录具体缺失，而不宣称已经有性能数据。

本 revision 的 metrics 使用单个选项（例如 `--metrics latency`），不是文档中的 `latency,throughput,memory` 组合。

```bash
CUDA_VISIBLE_DEVICES=2 PYTHONPATH=src .venv/bin/python -m pytest -q \
  benchmark/test_moe_align_block_size_triton.py \
  --level core --iter 10 --warmup 5 --mode kernel --metrics latency \
  --record json --output artifacts/benchmark.json
```

## 证据边界

- 128 个 quick correctness 通过只代表该测试覆盖，不等于所有真实 workload 正确。
- 尚未产生有效的真实 profiler trace，`profiler_verified=false`。
- 尚未标定 H20 的有效带宽/有效 FLOP/s，不能宣称达到理论极限。
- 后续 profile 仍需保持卡 2、固定 revision、短采集，并将原始输出、工具参数与地图节点关联。
