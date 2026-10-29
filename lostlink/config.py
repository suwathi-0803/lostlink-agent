"""Central configuration, loaded from .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("LOSTLINK_DB", str(DATA_DIR / "lostlink.db")))
CHECKPOINT_DB = Path(os.getenv("LOSTLINK_CHECKPOINTS", str(DATA_DIR / "checkpoints.db")))
OFFICER_TOKEN = os.getenv("OFFICER_TOKEN", "demo-officer-token")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
    "ollama": "qwen2.5:7b",
}
LLM_MODEL = os.getenv("LLM_MODEL", "").strip() or DEFAULT_MODELS.get(LLM_PROVIDER, "")

# A found item is auto-notified to the owner of a lost report when the match score is at least this.
AUTO_NOTIFY_THRESHOLD = int(os.getenv("AUTO_NOTIFY_THRESHOLD", "70"))
