"""Small boundary regression for oversized `topk_length` (issue #1324).

Run inside the vendor FlagGems-vllm environment, not in the local CPU venv.
The reference masks indices beyond the row width before evaluating attention.
"""
from __future__ import annotations

import os

os.environ.setdefault("GEMS_VENDOR", "metax")
os.environ.setdefault("FLAGGEMS_FLASHMLA_SPARSE_TLE", "0")

import torch

import flaggems_vllm
from flashmla_sparse_reference import reference_sparse_mla


def check_topk_length(topk_length):
    torch.manual_seed(42)
    s_q, h_q, d_qk, s_kv, topk = 2, 64, 512, 96, 64
    q = torch.randn(s_q, h_q, d_qk, device="cuda", dtype=torch.bfloat16) / 10
    kv = torch.randn(s_kv, 1, d_qk, device="cuda", dtype=torch.bfloat16) / 10
    indices = torch.randint(0, s_kv, (s_q, 1, topk), device="cuda", dtype=torch.int32)
    indices[0, 0, 0] = -1
    indices[0, 0, 1] = s_kv
    lengths = torch.tensor([topk_length, topk], device="cuda", dtype=torch.int32)
    output, max_logits, lse = flaggems_vllm.flash_mla_sparse_fwd(
        q, kv, indices, sm_scale=0.5, d_v=512, topk_length=lengths,
    )
    expected = reference_sparse_mla(q, kv, indices, 0.5, lengths)
    torch.testing.assert_close(output, expected[0], atol=8e-4, rtol=3.01 / 128)
    torch.testing.assert_close(max_logits, expected[1], atol=1e-6, rtol=2.01 / 65536)
    torch.testing.assert_close(lse, expected[2], atol=1e-6, rtol=2.01 / 65536)


def test_oversized_topk_length_stays_within_indices():
    for length in (0, 63, 64, 65, 4096):
        check_topk_length(length)
    print("5 boundary cases passed")


if __name__ == "__main__":
    test_oversized_topk_length_stays_within_indices()
