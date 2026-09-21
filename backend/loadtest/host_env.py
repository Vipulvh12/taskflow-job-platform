"""Points app settings at the Docker stack's published ports. Import this
before anything imports app.config.

The repo-root .env uses Compose hostnames (postgres:5432, redis:6379) that only
resolve inside the Compose network. .env.host holds the same values with the
host-side equivalents, including the same JWT_SECRET the api container verifies
tokens with. pydantic-settings gives environment variables precedence over the
.env file, so loading .env.host into the environment is enough."""
from pathlib import Path

from dotenv import load_dotenv

ENV_HOST = Path(__file__).resolve().parents[2] / ".env.host"

if not ENV_HOST.exists():
    raise SystemExit(f"{ENV_HOST} not found — the load test needs the host-side settings.")
load_dotenv(ENV_HOST, override=True)
