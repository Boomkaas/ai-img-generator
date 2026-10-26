"""Server-side validation of a submitted job.

Kept free of FastAPI so the worker and controller can import it; the API turns `ValidationProblem` into 422.
"""

from pydantic import BaseModel

from common.config import Settings, get_settings


class SubmitBody(BaseModel):
    prompt: str
    size: int = 512


class ValidationProblem(Exception):
    """A user-facing reason why a submitted job was refused."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def validate(body: SubmitBody, settings: Settings | None = None) -> str:
    """Return the cleaned prompt, or raise ValidationProblem."""
    settings = settings or get_settings()
    prompt = body.prompt.strip()
    if not prompt:
        raise ValidationProblem("The prompt is empty")
    if len(prompt) > settings.MAX_PROMPT_LEN:
        raise ValidationProblem(f"The prompt is longer than {settings.MAX_PROMPT_LEN} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in prompt):
        raise ValidationProblem("The prompt contains control characters")
    if body.size not in settings.ALLOWED_SIZES:
        raise ValidationProblem(f"Size must be one of {settings.ALLOWED_SIZES}")
    return prompt
