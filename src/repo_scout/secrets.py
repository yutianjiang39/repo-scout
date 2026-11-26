"""Credentials come from a plaintext private file, not environment variables.

Lookup order for a name such as GITHUB_READ_TOKEN:
  1. private/credentials.json next to this project, or the shared one in
     ../review-orchestrator/private/credentials.json (first file that exists);
     either a top-level key with the same name, or the mapped section/field below;
  2. the environment variable of the same name (servers without the file).
Never print or log returned values.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CANDIDATES = (
    PROJECT / "private" / "credentials.json",
    PROJECT.parent / "review-orchestrator" / "private" / "credentials.json",
)
ALIASES = {
    "GITHUB_READ_TOKEN": ("github", "read_token"),
    "GITHUB_WRITE_TOKEN": ("github", "write_token"),
    "ANTHROPIC_API_KEY": ("claude", "ANTHROPIC_AUTH_TOKEN"),
    "ANTHROPIC_AUTH_TOKEN": ("claude", "ANTHROPIC_AUTH_TOKEN"),
    "ANTHROPIC_BASE_URL": ("claude", "ANTHROPIC_BASE_URL"),
    "GEMINI_API_KEY": ("gemini", "key"),
    "OPENAI_API_KEY": ("gpt", "key"),
}


@lru_cache(maxsize=1)
def credentials() -> dict:
    for path in CANDIDATES:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def secret(name: str | None) -> str | None:
    if not name:
        return None
    data = credentials()
    value = data.get(name)
    if not isinstance(value, str) and name in ALIASES:
        section, field = ALIASES[name]
        value = (data.get(section) or {}).get(field)
    return value if isinstance(value, str) and value else os.environ.get(name) or None
