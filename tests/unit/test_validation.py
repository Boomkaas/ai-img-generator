import pytest

from common.config import Settings
from common.validation import SubmitBody, ValidationProblem, validate

pytestmark = pytest.mark.unit

SETTINGS = Settings(_env_file=None, JWT_SECRET="j" * 32)
MAX_PROMPT_LEN = SETTINGS.MAX_PROMPT_LEN


@pytest.mark.parametrize("size", SETTINGS.ALLOWED_SIZES)
def test_every_allowed_size_accepted(size):
    assert validate(SubmitBody(prompt="a cat", size=size), SETTINGS) == "a cat"


@pytest.mark.parametrize("size", [0, 100, 500, 1024, -1])
def test_other_sizes_rejected(size):
    with pytest.raises(ValidationProblem, match="Size must be one of"):
        validate(SubmitBody(prompt="a cat", size=size), SETTINGS)


@pytest.mark.parametrize("prompt", ["", "   ", "x" * (MAX_PROMPT_LEN + 1), "a\x00cat"])
def test_bad_prompts_rejected(prompt):
    with pytest.raises(ValidationProblem) as exc:
        validate(SubmitBody(prompt=prompt, size=512), SETTINGS)
    assert exc.value.message


def test_prompt_at_length_limit_accepted():
    assert validate(SubmitBody(prompt="x" * MAX_PROMPT_LEN, size=512), SETTINGS) == "x" * MAX_PROMPT_LEN


def test_prompt_is_stripped_and_keeps_newlines_and_tabs():
    assert validate(SubmitBody(prompt="  a\tcat\non a mat  ", size=512), SETTINGS) == "a\tcat\non a mat"


def test_default_size_is_512():
    assert SubmitBody(prompt="a cat").size == 512
