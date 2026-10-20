"""Every limit, timeout and connection setting in one place (architecture §4.1).

Values come from environment variables and `.env`. The derived timeouts follow the chain in §4.1 unless an
environment variable of the same name overrides them; the validator refuses any configuration that breaks
the order measured runtime < soft limit < hard limit < visibility timeout, hard limit < stop grace period.
"""

import socket
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Connections ---
    DATABASE_URL: str = "postgresql+psycopg://imagegen:imagegen@localhost:5432/imagegen"
    DB_POOL_SIZE: int = Field(5, ge=1)
    DB_POOL_TIMEOUT_S: float = Field(5, gt=0)  # wait for a free pooled connection before failing
    DB_CONNECT_TIMEOUT_S: int = Field(5, ge=2)  # libpq counts whole seconds and treats less than 2 as 2
    BROKER_URL: str = "redis://localhost:6379/0"
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CONNECT_TIMEOUT_S: float = Field(5, gt=0)
    REDIS_SOCKET_TIMEOUT_S: float = Field(5, gt=0)  # sync client only: pub/sub connections sit idle
    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "imagegen"
    S3_SECRET_KEY: str = "imagegen-dev-secret"
    S3_BUCKET: str = "imagegen"
    S3_REGION: str = "us-east-1"  # MinIO ignores it, but request signing needs one
    S3_STREAM_CHUNK_BYTES: int = Field(64 * 1024, ge=1)
    S3_CONNECT_TIMEOUT_S: float = Field(5, gt=0)
    S3_READ_TIMEOUT_S: float = Field(30, gt=0)
    S3_MAX_ATTEMPTS: int = Field(3, ge=1)  # the first try included

    # --- Auth ---
    JWT_SECRET: str = Field(min_length=32)  # HMAC key for HS256; at least 256 bits
    COOKIE_SECURE: bool = True
    ALLOWED_ORIGINS: Annotated[tuple[str, ...], NoDecode] = ("https://localhost",)
    ACCESS_TOKEN_TTL_S: int = Field(15 * 60, gt=0)
    REFRESH_TOKEN_TTL_S: int = Field(7 * 24 * 3600, gt=0)
    MIN_PASSWORD_LEN: int = Field(8, ge=1)
    MAX_EMAIL_LEN: int = Field(254, ge=3)  # the longest address SMTP allows
    # Each argon2 hash takes 64 MiB, so this bounds an API process's hashing memory however many sign in.
    MAX_CONCURRENT_PASSWORD_HASHES: int = Field(4, ge=1)

    # --- API process ---
    REQUEST_RATE_WINDOW_S: int = Field(60, gt=0)  # the rolling window behind requests_per_min (§4.8)

    # --- Worker ---
    MACHINE_NAME: str = Field(default_factory=socket.gethostname)
    PIPELINE: Literal["real", "fake"] = "real"
    FAKE_STEP_SECONDS: float = Field(1.0, ge=0)
    MODEL_DIR: str = "/models/sd15"
    WORKER_THREADS: int = Field(2, ge=1)
    STEP_DELAY_SECONDS: float = Field(0, ge=0)
    NUM_STEPS: int = Field(25, ge=1)

    # --- Limits on one task and on the number of tasks (§4.1, §4.3) ---
    ALLOWED_SIZES: tuple[int, ...] = (256, 384, 512, 640, 768)
    MAX_PROMPT_LEN: int = Field(500, ge=1)
    MAX_ACTIVE_TASKS_PER_USER: int = Field(1, ge=1)
    MAX_QUEUED_TOTAL: int = Field(30, ge=1)
    MAX_HISTORY_PER_USER: int = Field(50, ge=1)

    # --- Timing and estimates ---
    MEASURED_MAX_RUNTIME_S: int = Field(1080, gt=0)  # example; replaced after calibration (step 12)
    CALIBRATED_SECONDS_PER_COST_UNIT: float = Field(480, gt=0)
    ESTIMATE_SAMPLE_SIZE: int = Field(50, ge=1)  # recent done tasks the median is taken over (§4.6)
    ESTIMATE_MIN_SAMPLES: int = Field(5, ge=1)  # fewer than this: use the calibrated value

    # --- Heartbeats and admin panel (§4.8) ---
    HEARTBEAT_INTERVAL_S: float = Field(5, gt=0)
    HEARTBEAT_TTL_S: int = Field(15, ge=1)  # a process that stops writing disappears after this
    MAX_ALERTS: int = Field(20, ge=1)

    # --- Derived timeouts (§4.1); None means "derive from MEASURED_MAX_RUNTIME_S" ---
    TASK_SOFT_TIME_LIMIT: int | None = None
    TASK_TIME_LIMIT: int | None = None
    BROKER_VISIBILITY_TIMEOUT: int | None = None
    WORKER_STOP_GRACE_S: int | None = None
    CANCEL_FLAG_TTL_S: int | None = None
    WORKER_PROC_ALIVE_TIMEOUT: int = Field(180, gt=0)  # longer than loading the model

    # --- Controller / autoscaler (§4.7) ---
    SCALER: Literal["none", "swarm"] = "none"
    WORKER_SERVICE: str = "imagegen_worker"
    MIN_WORKERS: int = Field(1, ge=0)
    MAX_WORKERS: int = Field(4, ge=1)
    SCALE_UP_WAITING_PER_WORKER: int = Field(2, ge=1)
    COOLDOWN_S: int = Field(120, ge=0)
    STARTUP_TIMEOUT_S: int = Field(600, gt=0)
    SCALE_DOWN_IDLE_S: int = Field(300, ge=0)
    ALERT_WEBHOOK_URL: str = ""

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @model_validator(mode="after")
    def _derive_and_check(self):
        if self.TASK_SOFT_TIME_LIMIT is None:
            self.TASK_SOFT_TIME_LIMIT = int(self.MEASURED_MAX_RUNTIME_S * 1.5)
        if self.TASK_TIME_LIMIT is None:
            self.TASK_TIME_LIMIT = self.TASK_SOFT_TIME_LIMIT + 60
        if self.BROKER_VISIBILITY_TIMEOUT is None:
            self.BROKER_VISIBILITY_TIMEOUT = self.TASK_TIME_LIMIT * 2
        if self.WORKER_STOP_GRACE_S is None:
            self.WORKER_STOP_GRACE_S = self.TASK_TIME_LIMIT + 120
        if self.CANCEL_FLAG_TTL_S is None:
            self.CANCEL_FLAG_TTL_S = self.TASK_TIME_LIMIT

        # Each pair must be strictly increasing (§4.1).
        chain = [
            ("MEASURED_MAX_RUNTIME_S", "TASK_SOFT_TIME_LIMIT"),
            ("TASK_SOFT_TIME_LIMIT", "TASK_TIME_LIMIT"),
            ("TASK_TIME_LIMIT", "BROKER_VISIBILITY_TIMEOUT"),
            ("TASK_TIME_LIMIT", "WORKER_STOP_GRACE_S"),
        ]
        for lower, higher in chain:
            low, high = getattr(self, lower), getattr(self, higher)
            if not low < high:
                raise ValueError(f"timeout order broken: {lower} ({low}) must be less than {higher} ({high})")

        if self.MIN_WORKERS > self.MAX_WORKERS:
            raise ValueError(
                f"MIN_WORKERS ({self.MIN_WORKERS}) must not exceed MAX_WORKERS ({self.MAX_WORKERS})"
            )
        if not self.HEARTBEAT_INTERVAL_S < self.HEARTBEAT_TTL_S:
            raise ValueError(
                f"HEARTBEAT_INTERVAL_S ({self.HEARTBEAT_INTERVAL_S}) must be less than "
                f"HEARTBEAT_TTL_S ({self.HEARTBEAT_TTL_S})"
            )
        if not self.ALLOWED_SIZES:
            raise ValueError("ALLOWED_SIZES must not be empty")
        bad = [size for size in self.ALLOWED_SIZES if size <= 0 or size % 64]
        if bad:
            raise ValueError(f"ALLOWED_SIZES must be positive multiples of 64, got {bad}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
