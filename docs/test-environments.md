# 跨厂商测试环境登记

记录时间：2026-11-19。本文是后续 `triton_flash_mla_sparse_fwd` 测试的**环境来源与版本约束**，不是对这些环境已实测通过的声明。

## 使用原则

1. 每个测试必须关联 `environment_id`、实际镜像/容器、源码或 wheel 路径、组件版本、设备编号和 profiler 状态。
2. 镜像中的组件可能带厂商补丁，不能只记录 upstream revision；需要同时记录补丁分支、commit 或 wheel 哈希。
3. 实际 import 的代码优先于文档记录。若 `/opt`、wheel、`PYTHONPATH` 不一致，以运行时路径和内容哈希为准。
4. 不因镜像存在就跳过只读资源巡检；先选择空闲设备，再运行单卡小 case。
5. 不自动启动/停止生产服务、不改镜像、不杀进程、不 reset/锁频。
6. 不将任何 SSH 私钥、token、宿主凭据写入项目、镜像或 Agent prompt。

## 通用测试目标

- 仓库：`git@github.com:tonyh168/FlagGems-vllm.git`
- 已核查 upstream revision：`af184e5731c814883659f2e855209e14c349383d`
- Triton Kernel：`triton_flash_mla_sparse_fwd`
- 通用实现：`src/flaggems_vllm/ops/flashmla_sparse.py`
- Python 入口：`flash_mla_sparse_fwd`
- 正确性测试：`tests/test_flash_mla_sparse_fwd.py`
- 注意：厂商可能选择 TLE 或专用实现，必须记录实际 dispatch，不可只按 Python 名称判断。

## 1. MetaX / 沐曦

### environment_id

`metax-hy4-flash-20261118`

### 来源

`/Users/baai/Desktop/baai_proj/metax/auto-day0/models/metax/hy4-flash/pr_content.md`

文档说明日期：2026-11-18。当前仅阅读文档，尚未连接 `metax-119` 或运行该 sparse MLA 测试。

### 主机、容器与设备

- SSH：`metax-119`
- 容器：`hy4-flash`
- 硬件：8× MetaX C550 64GB。
- Python：`/flagos/bin/python`，3.12.13。
- torch：`2.10.0+metax3.8.1.0`。
- FlagTree：`0.7.0+metax3.6`。
- transformers：`5.17.0`。
- 测试前应检查容器是否仍在运行、空闲卡、实际镜像及 `MACA_VISIBLE_DEVICES`。
- 服务启动或新容器创建不属于默认 Kernel 测试动作；优先复用已存在且经过检查的环境。

### 组件

| 组件 | 版本 / revision |
|---|---|
| vLLM | `0.32.0+empty`，`vllm-project/vllm` tag `v0.32.0`，commit `5df63a7203cdb9328d824a27b682e77c7b9928be` |
| vllm-plugin-FL | 基线 `814b6d3e5ce7de251f9b503ff212bf3fd7cf3a48`；分支 `day0-hy4-flash-metax-v032`，补丁 `edfcc989` + `052bd4f2` |
| FlagGems | `0.0+g729c7e854`，`flagos-ai/FlagGems` **master** `729c7e854ebc589df4aabe4e0f79e1ce6e2f7954` |
| FlagGems-vllm | `0.3.0.dev1`，基线 `af184e5731c814883659f2e855209e14c349383d` |
| FlagGems-vllm 补丁 | 分支 `day0-hy4-flash-metax-v032`：`2afe6a69` IndexPool、`db18e8e1` PR #1291、`4fc7c13b` MetaX 修复 |
| graph 版本 | `day0-hy4-flash-metax-graph-20261118`，上述补丁加 `c8c34ba` capture-safe paged-MQA |

该环境还包含 Hy4-Flash 模型补丁与文档记录的 22 处 MetaX 适配。进行 Kernel 迭代时必须显式区分 upstream 基线和这些补丁，不可笼统写成“af184e57 原始代码”。

### 镜像

基底：

```text
harbor.baai.ac.cn/flagrelease-public/vllm0.24.0-metax-maca3.8.1.3:2.2.0-gems-main
sha256:bdd54081cf5cd2786ca097a5f9b4557191fd38f4bd054515decc6288482de574
```

Eager：

```text
harbor.baai.ac.cn/flagrelease-public/vllm0.24.0-metax-maca3.8.1.3:hy4-flash-metax-20261118
sha256:23335a15e47164c4d07fd8daebe734c151ec0ee9ca430728dd32469af48558fe
```

Graph（FULL_DECODE_ONLY）：

```text
harbor.baai.ac.cn/flagrelease-public/vllm0.24.0-metax-maca3.8.1.3:hy4-flash-metax-graph-20261118
sha256:02109a7a84f8bc81b5a7e81eb97f22cbe3c47074416e7f51f9774b4a25031614
```

### Profiling 与验证状态

- `mx-smi` 做设备巡检。
- 已在另一节点 `metax-57` 确认 `/opt/maca-3.7.1/bin/mcTracer` 存在；不能据此推断 `metax-119` 容器有相同路径。
- `metax-119` 的 mcTracer 路径、版本与采集参数需要在运行前单独检查。
- 文档记录 Hy4-Flash eager/graph 文本服务测试通过，不代表 sparse MLA Kernel 已测试，也不代表长上下文、VL 或全量 md5 已通过。

## 2. Hygon / 海光

### environment_id

`hygon-xingchen4-support-v024`

### 来源

`/Users/baai/Desktop/baai_proj/hygon/auto-day0/models/hygon/xingchen4/pr_content.md`

该文件是 XingChen4 的 PR/环境记录，**未给出可直接复用的镜像 tag、容器名、FlagGems-vllm 安装 revision 或 sparse MLA 对比配置**。

### 组件

| 组件 | 路径 / 版本 / revision |
|---|---|
| vllm-plugin-FL | `/workspace/vllm-plugin-FL`，`support-Xingchen4-hygon`，`6189ce2` |
| vllm-plugin-FL blacklist 修复参考 | `/workspace/vllm-plugin-FL`，`v0.3.0-dev-day0-qwen-next-metax`，`28acc58` |
| vLLM | `/workspace/vllm`，`releases/v0.24.0`，`ee0da84ab9e04ac7610e28580af62c365e898389`，`vllm-0.1.dev17936+gee0da84ab` |
| FlagGems | `/workspace/FlagGems`，detached HEAD `35f2943d818074179f49cdf8f26b858f33ac5aef`，tag `v5.4.0.dev0` |
| flash_attn | `2.8.3+das.opt1.dtk2604.torch2100.20260330.g3f0061` |
| 硬件 | 海光 HCU，`ttg.target=hip:gfx936`，文档服务验证为单机 TP=2 |
| 软件形态 | empty-vllm（无 `vllm._C` 扩展）+ FlagOS |

文档明确：vLLM 本体保持干净，MLA prefill 使用 `TRITON_MLA`；部分 dense-varlen 路径通过 DAS `flash_attn` fallback，另有 `_C_cache_ops` 补丁与前缀缓存/chunked-prefill 限制。

### 测试前必须补齐

- 实际镜像 tag/digest、容器和 SSH 别名。
- `FlagGems-vllm` 的真实安装路径、版本/revision、是否含 sparse MLA 实现。
- `torch`、Triton/FlagTree、DTK 的精确版本。
- 是否能调用 `flash_mla_sparse_fwd`，以及是否存在可用的参考实现。
- profiler：仅 `hy-smi` 不足以做 Kernel 归因，需要实际确认 DTK profiler。

`hygon-19` 已确认 `/opt/hyhal/bin/hy-smi` 与 `/opt/dtk`，但这不代表本环境或 sparse MLA 已验证。不能将 ROCm/NCU 参数直接套用到 Hygon。

## 3. 平头哥 / 真武 PPU

### environment_id

`ppu-tele285b-20261112`

### 来源

`/Users/baai/Desktop/baai_proj/zhenwu/auto-day0/models/zhenwu/tele-285B/pr_content.md`

文档日期：2026-11-12。当前只阅读文档，未连接 `ppu-167` 或启动容器。

### 主机、容器与设备

- SSH：`ppu-167`，文档主机 `bm-guoke-alish-zoneo1-810e-96g-0-167`。
- 容器：`guoke285b-c147-admission64-native16-key-tile-20261112`。
- 硬件：PPU 810E ×8；文档中物理卡 8–15 映射容器内 `CUDA_VISIBLE_DEVICES=0..7`。
- `GEMS_VENDOR=thead`，不能误写成 `ppu`。
- `/workspace/adaptation` 与 `/models` 为 bind mount，不在镜像内。
- 启动脚本：`/workspace/adaptation/launch.sh`；服务配置不是默认 Kernel 测试配置。

### 镜像

自包含交付镜像：

```text
harbor.baai.ac.cn/flagos-inner-models-release/285b-zhenwu810e-c147:tele-285b-admission64-native16-key-tile-20261112-full
manifest sha256:6ca618eea5b4c0ccd96d75a2c1a8c7db86371de923b1336aba99e720f6f9751a
image ID sha256:14fbfb4e63ee1b1e2d963793b99546b257035ecd79180a5d18d12a672fd725ed
```

运行时镜像：

```text
harbor.baai.ac.cn/flagos-inner-models-release/285b-zhenwu810e-c147:tele-285b-admission64-native16-key-tile-20261112
manifest sha256:f09bde8f62ed1bcdd89878b38bd2af5c0dc385be61ef8510e375e20e57e16250
image ID 6f54729b5fe2（文档简写）
```

基底：

```text
harbor.baai.ac.cn/flagos-inner-models-release/285b-zhenwu810e-c147@sha256:3b155901b4b4f4064ee13a26543d40a04622871ecb3b7399948059820b84c057
```

### 组件

| 组件 | 版本 |
|---|---|
| vLLM | `0.24.0+empty`，服务 fingerprint `vllm-0.24.0-tp8-9cd13837` |
| vllm-plugin-FL | `0.3.0.dev0+gcd3cca8767e5.ppu285b`，wheel sha256 前缀 `3266c801…4cb6` |
| FlagGems | `5.5.0.dev0+ga4ca511d137d.ppu285b`，wheel sha256 前缀 `63dcbd82…beb42` |
| FlagGems-vllm | `0.3.0.dev1+g1c2bea1e0136.ppu285b`，wheel sha256 前缀 `90c3d442…a0f9` |
| FlagTree | `0.7.0+ppu3.6` |
| PyTorch | `2.13.0` |
| PPU SDK | `202610070948` |
| 设备/驱动 | `ppu-smi 1.28`，Driver `2.1.1-rbd225` |

wheel 原始文件位置（文档记录）：

```text
/data/adaptation/285b-guoke-810e-20261110/wheels/
```

### 代码来源关键限制

- 三件套是 wheel 安装，不是容器内 Git checkout。
- `/opt/FlagGems-vllm` HEAD 为 `fa78ddee4c3e`，与 wheel `1c2bea1e0136` 不一致；不能用 `/opt` 版本替代 wheel 版本。
- wheel 中有 `runtime/backend/_thead/fused/flashmla_sparse.py`，含 PPU native_head16/key_tile 特化；`/opt` 对应位置未必有该文件。
- 文档指出这些 commit 不在公开 upstream 或已知 fork 分支上，构建分支/完整 40 位 SHA 未提供。测试时要记录实际 import 路径、文件内容哈希及 wheel 哈希，不能把结果直接归因于公开 upstream。
- PPU 原生实现与通用 `triton_flash_mla_sparse_fwd` 应作为不同候选/分支管理，避免错误比较。

### Profiling 与验证状态

- `ppu-smi` 负责设备巡检；Asight Systems/Compute 分别用于系统 timeline 与 Kernel 分析。
- 当前文档没有提供可直接复用的 Asight 采集命令，实际工具路径与参数需要在 `ppu-167` 上单独检查。
- 未验证 Asight、未验证 sparse MLA 单测、未验证当前容器是否可复用。

## 后续统一接入顺序

```text
读取本文件 → 只读检查实际主机/容器/设备 → 固定实际代码与组件指纹
→ 小 shape correctness → 短 baseline → 真实 profiler
→ 记录 environment_id 与实际差异 → 才允许进入迭代地图
```

优先使用 MetaX `metax-hy4-flash-20261118` 做 sparse MLA 的首个真实测试，因为它与目标模型/算子和补丁链最接近。Hygon 需要补齐环境信息；PPU 必须按 wheel 中的专用实现建立独立分支。

相关指导：`docs/hardware-profiling.md`、`docs/performance-model.md`、`.claude/skills/kernel-profiling/SKILL.md`。
