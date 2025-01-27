import argparse
import time
import torch
from PIL import Image

from onediff.quantization import QuantPipeline


parser = argparse.ArgumentParser()
parser.add_argument(
    "--model", type=str, default="stabilityai/stable-diffusion-xl-base-1.0"
)
parser.add_argument("--variant", type=str, default="fp16")
parser.add_argument("--quantized_model", type=str, required=True)
parser.add_argument("--height", type=int, default=1024)
parser.add_argument("--width", type=int, default=1024)
parser.add_argument("--steps", type=int, default=30)
parser.add_argument(
    "--prompt",
    type=str,
    default="Astronaut in a jungle, cold color palette, muted colors, detailed, 8k",
)
parser.add_argument("--input_image", type=str, default=None)
parser.add_argument(
    "--conv_compute_density_threshold",
    type=int,
    default=900,
    help="The conv modules whose computational density is higher than the threshold will be quantized.",
)
parser.add_argument(
    "--linear_compute_density_threshold",
    type=int,
    default=300,
    help="The linear modules whose computational density is higher than the threshold will be quantized.",
)
parser.add_argument(
    "--conv_ssim_threshold",
    type=float,
    default=0,
    help="A similarity threshold that quantize convolution. The higher the threshold, the lower the accuracy loss caused by quantization.",
)
parser.add_argument(
    "--linear_ssim_threshold",
    type=float,
    default=0,
    help="A similarity threshold that quantize linear. The higher the threshold, the lower the accuracy loss caused by quantization.",
)
parser.add_argument(
    "--save_as_float",
    default=False,
    type=(lambda x: str(x).lower() in ["true", "1", "yes"]),
)
parser.add_argument("--seed", type=int, default=111)
parser.add_argument("--cache_dir", type=str, default=None)
args = parser.parse_args()


if args.input_image is None:
    from diffusers import AutoPipelineForText2Image as pipeline_cls
else:
    from diffusers import AutoPipelineForImage2Image as pipeline_cls

pipe = QuantPipeline.from_pretrained(
    pipeline_cls,
    args.model,
    torch_dtype=torch.float16,
    variant=args.variant,
    use_safetensors=True,
)
pipe.to("cuda")

pipe_kwargs = dict(
    prompt=args.prompt,
    height=args.height,
    width=args.width,
    num_inference_steps=args.steps,
    seed=args.seed,
)
if args.input_image is not None:
    from diffusers.utils import load_image

    input_image = load_image(args.input_image)
    pipe_kwargs["image"] = input_image.resize((args.width, args.height), Image.LANCZOS)

start_time = time.time()

pipe.quantize(
    **pipe_kwargs,
    conv_compute_density_threshold=args.conv_compute_density_threshold,
    linear_compute_density_threshold=args.linear_compute_density_threshold,
    conv_ssim_threshold=args.conv_ssim_threshold,
    linear_ssim_threshold=args.linear_ssim_threshold,
    save_as_float=args.save_as_float,
    cache_dir=args.cache_dir,
)

pipe.save_quantized(args.quantized_model, safe_serialization=True)

end_time = time.time()

print(f"Quantize module time: {end_time - start_time}s")
