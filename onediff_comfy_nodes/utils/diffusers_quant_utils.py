import os
import comfy
import torch
import torch.nn as nn
from diffusers_quant.utils import get_quantize_module
from diffusers_quant.models import StaticQuantLinearModule, DynamicQuantLinearModule
from torch._dynamo import allow_in_graph as maybe_allow_in_graph

__all__ = ["replace_module_with_quantizable_module"]


def _use_graph():
    os.environ["with_graph"] = "1"
    os.environ["ONEFLOW_GRAPH_DELAY_VARIABLE_OP_EXECUTION"] = "1"
    os.environ["ONEFLOW_MLIR_CSE"] = "1"
    os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
    os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
    os.environ["ONEFLOW_MLIR_FUSE_FORWARD_OPS"] = "1"
    os.environ["ONEFLOW_MLIR_FUSE_OPS_WITH_BACKWARD_IMPL"] = "1"
    os.environ["ONEFLOW_MLIR_GROUP_MATMUL"] = "1"
    os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"
    os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_CONV_BIAS"] = "1"
    os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"
    os.environ["ONEFLOW_KERNEL_CONV_CUTLASS_IMPL_ENABLE_TUNING_WARMUP"] = "1"
    os.environ["ONEFLOW_KERNEL_CONV_ENABLE_CUTLASS_IMPL"] = "1"
    os.environ["ONEFLOW_KERNEL_GEMM_CUTLASS_IMPL_ENABLE_TUNING_WARMUP"] = "1"
    os.environ["ONEFLOW_KERNEL_GEMM_ENABLE_CUTLASS_IMPL"] = "1"
    os.environ["ONEFLOW_CONV_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"
    os.environ["ONEFLOW_MATMUL_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"
    os.environ["ONEFLOW_LINEAR_EMBEDDING_SKIP_INIT"] = "1"
    os.environ["ONEFLOW_KERNEL_GLU_ENABLE_DUAL_GEMM_IMPL"] = "0"
    os.environ["ONEFLOW_MLIR_GROUP_MATMUL_QUANT"] = "1"
    os.environ["ONEFLOW_FUSE_QUANT_TO_MATMUL"] = "0"
    # os.environ["ONEFLOW_MLIR_FUSE_KERNEL_LAUNCH"] = "1"
    # os.environ["ONEFLOW_KERNEL_ENABLE_CUDA_GRAPH"] = "1"


def get_sub_module(module, sub_module_name) -> nn.Module:
    """Get a submodule of a module using dot-separated names.

    Args:
        module (nn.Module): The base module.
        sub_module_name (str): Dot-separated name of the submodule.

    Returns:
        nn.Module: The requested submodule.
    """

    parts = sub_module_name.split(".")
    current_module = module

    for part in parts:
        try:
            if part.isdigit():
                current_module = current_module[int(part)]
            else:
                current_module = getattr(current_module, part)
        except (IndexError, AttributeError):
            raise ModuleNotFoundError(f"Submodule {part} not found.")

    return current_module


def modify_sub_module(module, sub_module_name, new_value):
    """Modify a submodule of a module using dot-separated names.

    Args:
        module (nn.Module): The base module.
        sub_module_name (str): Dot-separated name of the submodule.
        new_value: The new value to assign to the submodule.

    """
    parts = sub_module_name.split(".")
    current_module = module

    for i, part in enumerate(parts):
        try:
            if part.isdigit():
                if i == len(parts) - 1:
                    current_module[int(part)] = new_value
                else:
                    current_module = current_module[int(part)]
            else:
                if i == len(parts) - 1:
                    setattr(current_module, part, new_value)
                else:
                    current_module = getattr(current_module, part)
        except (IndexError, AttributeError):
            raise ModuleNotFoundError(f"Submodule {part} not found.")


def _load_calibrate_info(calibrate_info_path):
    calibrate_info = {}
    with open(calibrate_info_path, "r") as f:
        for line in f.readlines():
            line = line.strip()
            items = line.split(" ")
            calibrate_info[items[0]] = [
                float(items[1]),
                int(items[2]),
                [float(x) for x in items[3].split(",")],
            ]
    return calibrate_info


def search_modules(root, match_fn: callable, name=""):
    """
    example:
    >>> search_modules(model, lambda m: isinstance(m, (nn.Conv2d, nn.Linear))
    """
    if match_fn(root):
        return {name: root}

    result = {}
    for child_name, child in root.named_children():
        result.update(
            search_modules(
                child, match_fn, f"{name}.{child_name}" if name != "" else child_name
            )
        )
    return result


def _can_use_flash_attn(attn):
    dim_head = attn.to_q.out_features // attn.heads
    if dim_head != 40 and dim_head != 64:
        return False
    if attn.to_k is None or attn.to_v is None:
        return False
    if (
        attn.to_q.bias is not None
        or attn.to_k.bias is not None
        or attn.to_v.bias is not None
    ):
        return False
    if (
        attn.to_q.in_features != attn.to_k.in_features
        or attn.to_q.in_features != attn.to_v.in_features
    ):
        return False
    if not (
        attn.to_q.weight.dtype == attn.to_k.weight.dtype
        and attn.to_q.weight.dtype == attn.to_v.weight.dtype
    ):
        return False
    return True


def _rewrite_attention(attn):
    dim_head = attn.to_q.out_features // attn.heads
    has_bias = attn.to_q.bias is not None
    attn.to_qkv = nn.Linear(
        attn.to_q.in_features, attn.to_q.out_features * 3, bias=has_bias
    )
    attn.to_qkv.requires_grad_(False)
    qkv_weight = torch.cat(
        [
            attn.to_q.weight.permute(1, 0).reshape(-1, attn.heads, dim_head),
            attn.to_k.weight.permute(1, 0).reshape(-1, attn.heads, dim_head),
            attn.to_v.weight.permute(1, 0).reshape(-1, attn.heads, dim_head),
        ],
        dim=2,
    )
    qkv_weight = (
        qkv_weight.reshape(-1, attn.to_q.out_features * 3).permute(1, 0).contiguous()
    )
    attn.to_qkv.weight.data = qkv_weight

    if has_bias:
        qkv_bias = (
            torch.cat(
                [
                    attn.to_q.bias.reshape(attn.heads, dim_head),
                    attn.to_k.bias.reshape(attn.heads, dim_head),
                    attn.to_v.bias.reshape(attn.heads, dim_head),
                ],
                dim=1,
            )
            .reshape(attn.to_q.out_features * 3)
            .contiguous()
        )
        attn.to_qkv.bias.data = qkv_bias

    if isinstance(attn.to_q, StaticQuantLinearModule) or isinstance(
        attn.to_q, DynamicQuantLinearModule
    ):
        cls = type(attn.to_q)
        weight_scale = (
            torch.cat(
                [
                    torch.Tensor(attn.to_q.calibrate[2]).reshape(attn.heads, dim_head),
                    torch.Tensor(attn.to_k.calibrate[2]).reshape(attn.heads, dim_head),
                    torch.Tensor(attn.to_v.calibrate[2]).reshape(attn.heads, dim_head),
                ],
                dim=1,
            )
            .reshape(attn.to_q.out_features * 3)
            .contiguous()
        )
        calibrate = [attn.to_q.calibrate[0], attn.to_q.calibrate[1], weight_scale]

        old_env = os.getenv("ONEFLOW_FUSE_QUANT_TO_MATMUL")
        os.environ["ONEFLOW_FUSE_QUANT_TO_MATMUL"] = "0"
        attn.to_qkv = cls(attn.to_qkv, attn.to_q.nbits, calibrate, attn.to_q.name)
        attn.scale = dim_head**-0.5

        os.environ["ONEFLOW_FUSE_QUANT_TO_MATMUL"] = old_env


def replace_module_with_quantizable_module(diffusion_model, calibrate_info_path):
    _use_graph()

    calibrate_info = _load_calibrate_info(calibrate_info_path)
    for sub_module_name, sub_calibrate_info in calibrate_info.items():
        sub_mod = get_sub_module(diffusion_model, sub_module_name)

        if isinstance(sub_mod, comfy.ops.Linear):
            # fix diffusers_quant use isinstance(sub_mod, torch.nn.Linear)
            sub_mod.__class__ = torch.nn.Linear

        sub_mod.weight.requires_grad = False
        sub_mod.weight.data = sub_mod.weight.to(torch.int8)
        sub_mod.cuda()  # TODO: remove this line , because we diffusers_quant pkg weight_scale
        sub_mod = get_quantize_module(
            sub_mod,
            sub_module_name,
            sub_calibrate_info,
            fake_quant=False,
            static=False,
            nbits=8,
            convert_fn=maybe_allow_in_graph,
        )
        modify_sub_module(diffusion_model, sub_module_name, sub_mod)

    try:
        # rewrite CrossAttentionPytorch to use qkv
        from comfy.ldm.modules.attention import CrossAttentionPytorch

        match_func = lambda m: isinstance(
            m, CrossAttentionPytorch
        ) and _can_use_flash_attn(m)
        can_rewrite_modules = search_modules(diffusion_model, match_func)
        print(f"rewrite {len(can_rewrite_modules)=} CrossAttentionPytorch")
        for k, v in can_rewrite_modules.items():
            if f"{k}.to_q" in calibrate_info:
                _rewrite_attention(v)  # diffusion_model is modified in-place
            else:
                print(f"skip {k+'.to_q'} not in calibrate_info")

    except Exception as e:
        print(e)