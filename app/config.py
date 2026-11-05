"""Central configuration, read from environment variables or a .env file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://medmatch:medmatch@localhost:5432/medmatch"
)
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "medmatch123")
USE_NEO4J = os.getenv("USE_NEO4J", "true").lower() in ("1", "true", "yes")

# Seconds a waiting (older) transaction is allowed to block before giving up
LOCK_WAIT_TIMEOUT = float(os.getenv("LOCK_WAIT_TIMEOUT", "5"))
