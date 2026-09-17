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

    # Total tries per job, including the first. 3 = one attempt plus two
    # retries, so job_retry_delays needs (job_max_attempts - 1) entries.
    job_max_attempts: int = 3
    # Comma-separated rather than a list, because reading a list from an
    # env var means JSON-encoding it there. Exponential: 5s, then 25s.
    job_retry_delays: str = "5,25"

    @property
    def retry_delays(self) -> list[int]:
        return [int(part) for part in self.job_retry_delays.split(",") if part.strip()]

    def delay_for_attempt(self, attempt_number: int) -> int:
        """Delay before the attempt that follows `attempt_number`. Clamps to
        the last configured tier if more retries than tiers are configured."""
        delays = self.retry_delays
        return delays[min(attempt_number, len(delays)) - 1]


settings = Settings()
