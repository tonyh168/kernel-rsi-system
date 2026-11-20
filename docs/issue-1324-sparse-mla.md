# Issue #1324：MetaX sparse MLA `topk_length` 越界调查与修复

Issue：https://github.com/flagos-ai/FlagGems-vllm/issues/1324

调查日期：2026-11-19。

## 结论

通用 Triton sparse MLA Kernel 在 `HAVE_TOPK_LENGTH=True` 时，直接使用上游 `topk_length` 控制索引循环，但没有将它限制在 `indices` 的实际行宽 `TOPK` 内。

```python
topk_len = tl.load(topk_length_ptr)
NK = tl.cdiv(topk_len, BK)
t_msk = t_ptr < topk_len
kv_ids = tl.load(t_ptr, t_msk, other=-1)
```

当 `topk_length[i] > TOPK` 时，会读取 `indices[i, 0, TOPK:]`，末行可能越过整个 allocation，触发 Xnack/ATU fault。

最小修复是将动态长度 clamp 到 `TOPK`。这保持了 `topk_length=None` 时本来就只允许处理一行 `TOPK` 个索引的语义。

## 真实环境

- 主机：`metax-113`，8× MetaX C550。
- 独立容器：`flaggems-rsi-issue1324`，未停止或修改已有服务。
- 镜像：`harbor.baai.ac.cn/flagrelease-public/vllm0.24.0-metax-maca3.8.1.3:hy4-flash-metax-graph-20261118`。
- 镜像 ID：`sha256:02109a7a84f8bc81b5a7e81eb97f22cbe3c47074416e7f51f9774b4a25031614`。
- Kernel 源码：`day0-hy4-flash-metax-graph-20261118`，revision `c8c34bab8f9ae2e9707e2812164f67e1bd406e50`。
- torch：`2.10.0+metax3.8.1.0`；MACA 3.8.1.3；kernel driver 3.9.25；mx-smi 2.3.4。
- `/flagos/bin/python`；Triton 来源 `/opt/flagtree`。
- 单卡 `MACA_VISIBLE_DEVICES=0`，`FLAGGEMS_FLASHMLA_SPARSE_TLE=0` 用于复现通用 JIT 路径。

## 修改前证据

Safe case：

```text
q=(4096, 64, 576) kv=(16384, 1, 576) indices=(4096, 1, 2048)
topk_length=2048 (TOPK=2048)
OK out=(4096, 64, 512) lse=(4096, 64)
```

Oversized case：

```text
q=(4096, 64, 576) kv=(16384, 1, 576) indices=(4096, 1, 2048)
topk_length=4096 (TOPK=2048)
kernelName: triton_flash_mla_sparse_fwd
trapType: Xnack Error/ATU Fault(0x8)
CUDA error: an illegal memory access was encountered
```

## Patch

当前工作副本 patch 保存在：

- `examples/issue1324/flashmla_sparse.py`（修改后完整源码）。
- `examples/issue1324/flashmla_sparse.original.py`（修改前完整源码）。
- `artifacts/issue1324/issue1324.patch`（由隔离 Git 工作副本导出）。

通用 JIT 路径：

```diff
-    topk_len = tl.load(topk_length_ptr) if HAVE_TOPK_LENGTH else TOPK
+    # The dynamic count must never read beyond the physical indices row.
+    topk_len = tl.minimum(tl.load(topk_length_ptr), TOPK) if HAVE_TOPK_LENGTH else TOPK
```

另对同一文件中 TLE producer/consumer 的三处 `topk_len_ptr` 读取增加相同的 `tl.minimum(..., TOPK)`，避免替代路径出现同类越界。**这些 TLE 改动尚未在支持 TLE 的设备上实测，不能视为 TLE 路径已验证。**

PPU `_thead` 实现已经使用 `tl.minimum(tl.maximum(..., 0), TOPK)`，本 issue 的通用修复不覆盖或替代该专用实现。

## 验证结果

| 测试 | 结果 |
|---|---|
| 原始 safe case，TOPK=2048 | PASS |
| 原始 oversized case，topk_length=4096 > TOPK=2048 | 修改前 ATU fault；通用 clamp 后 PASS，输出 shape 正确 |
| 小型 reference 对比，长度 0/63/64/65/4096 | 通用 clamp 后 5 个 case 全部通过 |
| 无效索引 `-1` 和 `>= s_kv` | 小 case 中保持 reference 屏蔽语义 |
| 公开 sparse MLA quick suite | **444 passed, 19 warnings in 260.17s**；见 `sparse-quick.log` |

回归脚本：

- `examples/issue1324/test_sparse_mla_topk_bounds.py`
- `examples/issue1324/flashmla_sparse_reference.py`

小测试使用 `S_Q=2, H_Q=64, D_QK=512, S_KV=96, TOPK=64`，对输出、max_logits、lse 与独立 PyTorch reference 做比较，避免为每个边界条件重复大规模 serving。

## PR 与 upstream 验证（2026-11-19 更新）

已创建 PR：https://github.com/flagos-ai/FlagGems-vllm/pull/1339

- 分支：`tonyh168:fix-1324-sparse-mla-topk-bounds`
- commit：`d691ca26d5fef60059a0764d429912e1ead1724e`
- base：`flagos-ai/FlagGems-vllm` `main`（验证时 `b04b177`）。
- 包含动态 topk clamp、TLE 同类防御、64 KB 小 tile fallback 和正式回归测试。

upstream 全量 sparse quick suite：**449 passed, 19 warnings in 2664.70s**。目标边界测试：**5 passed, 444 deselected**。

此前 upstream 的 64x64 tiles 在该 MetaX C550 上超出 64 KB shared memory，造成 `OutOfResources`，所以 PR 同时包含已验证的小 tile fallback，而没有只提交无法在当前厂商环境运行的 clamp。TLE 路径仍未实测，TP8 serving 根因仍需完整验证。

验证日志：`artifacts/issue1324/pr1339-sparse-quick.log`、`artifacts/issue1324/pr1339-boundary.log`。

## 尚未证明的事项

1. 此修复证明了 oversized 动态长度导致的确定性 Kernel 越界可以消除；**没有证明线上 TP8 并发崩溃全部由这一原因导致**。
2. 上游 `valid_topk_counts` 是否在 serving 时确实 oversized，需要另外调查生产缓冲区的 producer/consumer 生命周期。
3. 尚未在原 Hy4-Flash TP8 模型上进行 64 并发与前缀缓存命中验证，不能声称 serving 问题完全关闭。
4. TLE 与 PPU 路径需要各自的环境验证，不能从 MetaX 通用 JIT 成功推断。
5. 没有自动 commit、push、PR 或修改 `metax-119` 的现有生产容器。
