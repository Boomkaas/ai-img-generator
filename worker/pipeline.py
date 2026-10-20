"""The Stable Diffusion pipeline, or a fake with the same call for tests and CI.

torch and diffusers are imported only in the real branch, so the fake runs without them installed.
"""

import time
from types import SimpleNamespace

from PIL import Image

from common.config import get_settings


def load_pipeline():
    settings = get_settings()
    if settings.PIPELINE == "fake":
        return FakePipeline(step_seconds=settings.FAKE_STEP_SECONDS)

    import torch
    from diffusers import StableDiffusionPipeline

    torch.set_num_threads(settings.WORKER_THREADS)
    pipe = StableDiffusionPipeline.from_pretrained(settings.MODEL_DIR, torch_dtype=torch.float32).to("cpu")
    pipe.enable_attention_slicing()  # caps peak RAM at 768 px
    pipe.set_progress_bar_config(disable=True)
    return pipe


class FakePipeline:
    """Same call as the real pipeline, but sleeps instead of computing."""

    def __init__(self, step_seconds: float):
        self.step_seconds = step_seconds

    def __call__(self, prompt, num_inference_steps, height, width, callback_on_step_end):
        for step in range(num_inference_steps):
            time.sleep(self.step_seconds)
            callback_on_step_end(self, step, 0, {})
        return SimpleNamespace(images=[Image.new("RGB", (width, height), "gray")])
