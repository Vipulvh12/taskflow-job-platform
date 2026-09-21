from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# backend/app/config.py -> parents[2] is the repo root (taskflow/), where .env lives
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    redis_url: str
    rabbitmq_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Postgres connections per process — SQLAlchemy's own defaults, made
    # explicit because the API's in-flight request limit is derived from them
    # (see app/concurrency_limit.py).
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Total tries per job, including the first. 3 = one attempt plus two
    # retries, so job_retry_delays needs (job_max_attempts - 1) entries.
    job_max_attempts: int = 3
    # Comma-separated rather than a list, because reading a list from an
    # env var means JSON-encoding it there. Exponential: 5s, then 25s.
    job_retry_delays: str = "5,25"

    # Where handlers write generated artifacts. Local disk for MVP; in Phase 13
    # this becomes a volume shared by the api and worker containers, and in V2
    # it is replaced by object storage (MinIO / S3).
    storage_dir: Path = ROOT_DIR / "storage"

    # Browser origins allowed to call this API. Configurable because Vite picks
    # the next free port if 5173 is taken, and a mismatch here fails as an
    # opaque browser-side CORS error with nothing useful in the server log.
    cors_origins_raw: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    @property
    def retry_delays(self) -> list[int]:
        return [int(part) for part in self.job_retry_delays.split(",") if part.strip()]

    def tier_for_attempt(self, attempt_number: int) -> int:
        """Which retry tier schedules the attempt following `attempt_number`.
        Clamps to the last tier if max_attempts exceeds the configured delays."""
        return min(attempt_number, len(self.retry_delays))

    def delay_for_attempt(self, attempt_number: int) -> int:
        """The wait that tier will impose. Used for logging — the authoritative
        delay is the queue's x-message-ttl, set at declaration."""
        return self.retry_delays[self.tier_for_attempt(attempt_number) - 1]


settings = Settings()
