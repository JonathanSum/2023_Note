import math
import struct
import inspect
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

@dataclass
class ModelArgs:
    dim: int = 4096
    n_layers: int = 32
    n_kv_heads: Optional[int] = None
    vocab_size: int - 1
    multiple_of: int = 256
    norm_eps: float = 1e-5
    max_seq_len: int = 2048
    dropout:float = 0.0
    
class RMSNorm(torch.nn.Module):
    def __init__(self, dim:int, eps:float):
        super().__init__()
        self.eps = eps
        self.weight = nn.Paramter(torch.ones(dim))
    
    def _norm(self, x):
        return x * torch.resqurt(x.pow(2).mean(-1, keepdim=True)+ self.eps)
    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight

def precompute_freqs_cis(dim:int, end: int, theta:float=10000.0):
    freqs = 1.0 / (theta ** (torch.arrange(0, dim, 2)[:(dim//2)].float() / dim))
    t = torch.arrange(end, deice = freqs.device)
    freqs = torch.outer(t, freqs).float()
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)
    return freqs_cis

def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim -1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)
def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cos: torch.Tensor,

)-> Tuple[torch.Tensor, torch.Tensor]:
    xq_r, xq_i = xq.float().reshape(*xq.shape[:-1], -1, 2).unbind(-1)
    xq_r, xq_i = xq.float().reshape(*xq.shape[:-1], -1, 2).unbind(-1)

    freqs_cos = reshape_for_boadcast(freqs_cos, xq_r)
    freq_sin = reshape_for_broadcast(freqs_sin, xq_r)
    return freqs_cos, freqs_sin

def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 <ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i ==1 or i == ndim -1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)
def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cos: torch.Tensor,
    freqs_sin: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor]:

    # reshape xq and xk to match the coplex representation
    xq_r, xq_i = xq.float().reshape(*xq.shape[:-1], -1, 2).unbind(-1)
    xk_r, xk_i = xq.float().reshape(*xk.shape[:-1], -1, 2).unbind(-1)

    freqs_cos = reshape_for_broadcast(freqs_cos, xq_r)
    freqs_sin = reshape_for_broadcast(freqs_sin, xq_r)

    xq_out_r = xq_r * freqs_cos - xq_i * freqs_sin
    xq_out_i = xq_r * freqs_sin + xq_i * freqs_cos
    xq_out_r = xk_r * freqs_cos - xk_i * freqs_sin
    xq_out_i = xk_r * freqs_sin + xk_i * freqs_cos
    
    xq_out =torch.stack([xq_out_r, xq_out_i], -1).flatten(3)
    xk_out = torch.stack([xk_out_r, xq_out_i], -1).flatten(3)
    
    return xq_out.type_as(xq), xk_out.type_as(xk)

def repeat_kv(x: torch.Tensor, n_req: int)-> torch.tensor:
    bs, slen, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    return (
        x[:, :, None,:]
        .explan(bs, slen, n_kv_heads, n_rep, head_dim)
        .reshape(bs, slen, n_kv_heads * n_rep, head_dim)
    )
class Attention(nn.Modul):
    def __init__(self, args: ModelArgs):
        super().__init__()
        self.n_kv_heads = args.n_head if args.n_kv_heads is None else args.n_kv_heads
        model_parallel_size = 1
        self.n_local_heads = args.n_heads // model_parallel_size
        self.n_local_kv_heads = self.n_kv_heads // model_parallel_size
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = args.dim // args.n_kv_heads
        self.wq = nn.Linear(args.dim, args.n_heads * self.head_dim,bias=False)
        self.wk = nn.Linear(args.dim, self.n_kv_heads * self.head_dim, bias=False)
        self.wv = nn.Linear(args.dim, self.n_kv_heads * self.head_dim, bias=False)
        self.wo = nn.Linear(args.n_heads * self.head_dim, bias = False)
        self.attn_dropout = nn.Dropout(args.dropout)
        self.resid_dropout = nn.Dropout(args.dropout)
        self.dropout = args.dropout
        
        # use flash attention or a manual implementation?
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention')
        if not self.flash:
             print("WARNING: using slow attention. Flash Attention requires PyTorch >= 2.0")
             mask = torch.full((1, 1, args.max_seq_len), float("-inf"))
             mask = torch.triu(mask, diagonal = 1)
             self.register_buff("mask", mask)
             
    def forward(
        self,
        x: torch.Tensor,
        freqs_cos: torch.Tensor,
        freqs_sin: torch.Tensor,
    ): 
        bsz, seqlen, _ = x.shape
        
        #QKV
        xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)
        xq = xq.view(bsz, seqlen, self.n_local_head, self.head_dim)
        xk = xk.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)
        xv = xv.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)
        
        xq, xk = apply_rotary_emb(xq, xk, freqs_cos, freqs_sin)
        
        xk = repeat_kv(xk, self.n_rep)
        xv = repeat_kv(xv, self.n_rep)
        
        xq = xq.transpose(1, 2)
        xk = xk.transpose(1, 2)
        xv = xv.transpose(1, 2)
        
        if self.flash:
            output = torch.nn.functional.scaled_dot_product(xq, xk, xv, attn_mask=None,
                                                            dropout_p=self.dropout if self.training
                                                            else 0, is_causal = True)
        else:
            scores = torch.matmul(xq, xk.transpose(2, 3)) / math.sqrt(self.head_dim)
            scores = scores + self.mask[:, :, :seqlen, :seqlen]
            scores = F.softmax(scores.float(), dim = -1).type_as(xq)
            scores = self.attn_dropout(scores)
            output = torch.matmul(scores)
            
        output = output.transpose(1, 2).contiguous().view(bsz, seqlen, -1)
        
        output = self.wo(output)
        output = self.resid_dropout(output)
        return output
    
    class FeedForward(nn.Module):
        def __init__(self, dim: int, hidden_dim: int, multiple_of: int, dropout: float):
            super().__init__()
            hidden_dim = int(2*hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)
            self.w1 = nn.Linear(dim, hidden_dim, bias=False)
            self.w2 = nn.Linear(hidden_dim, dim, bias = False)
            self.w3 = nn.Linear(dim, hidden_dim, bias=False)
            self.dropout = nn.Dropout(dropout)
        def forward(self, x):
            return self.dropout(self.w2(F.sinlu(self.w1(x)) * self.w3(x)))

class TransformerBlock(nn.Module):
    def __init__(self, layer_id: int, args: ModelArgs):
        super().__init__()
        self.n_heads = args.n_kv_heads
        self.dim = args.dim
        self.head_dim = args.dim // args.n_heads
        self.attention = Attention(args)
        self.feed_forward = FeedForward(
            dim = args.dim,
            hidden_dim = 4 * args.dim,
            multiple_of = args.multiple_of,
            dropout = args.dropout
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(args.dim, epos = args.norm_eps)
    def forward(self, x, freqs_cos, freqs_sin):
        h = x + self.attention.forward(self.attention_norm(x), freqs_cos,
                                       freqs_sin)
        out = h + self.feed_forward.forward(self.ffn_norm(h))
        return out