"""Download the Stable Diffusion 1.5 weights the worker loads, in diffusers layout, into a directory.

    python download_model.py <model id> <revision> <directory>

Runs at image build time. Only the full-precision safetensors files are fetched: the repository also holds
fp16 variants, .bin copies and single-file checkpoints that the CPU pipeline never reads.
"""

import shutil
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

PATTERNS = [
    "model_index.json",
    "*/*.json",
    "*/*.txt",
    "*/model.safetensors",
    "*/diffusion_pytorch_model.safetensors",
]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    model_id, revision, directory = argv
    snapshot_download(model_id, revision=revision, local_dir=directory, allow_patterns=PATTERNS)
    shutil.rmtree(Path(directory) / ".cache", ignore_errors=True)  # download bookkeeping, not weights
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
