"""Small PyTorch reference used only by the issue #1324 regression test."""
from __future__ import annotations

import torch


def reference_sparse_mla(q, kv, indices, sm_scale, topk_length):
    s_q, h_q, d_qk = q.shape
    topk = indices.shape[-1]
    indices = indices.clone().squeeze(1)
    mask = torch.arange(topk, device=q.device)[None, :] >= topk_length.clamp(0, topk)[:, None]
    indices[mask] = -1
    invalid = (indices < 0) | (indices >= kv.shape[0])
    safe_indices = indices.masked_fill(invalid, 0)
    gathered = kv.index_select(0, safe_indices.flatten()).reshape(s_q, topk, d_qk).float()
    scores = (q.float() @ gathered.transpose(1, 2)) * sm_scale
    scores = scores.masked_fill(invalid[:, None, :], float("-inf"))
    max_logits = scores.max(dim=-1).values
    lse = torch.logsumexp(scores, dim=-1)
    lse_for_output = lse.masked_fill(lse == float("-inf"), float("+inf"))
    output = torch.exp(scores - lse_for_output[..., None]) @ gathered[..., :512]
    lse = lse.masked_fill(lse == float("-inf"), float("+inf"))
    return output.to(torch.bfloat16), max_logits, lse
