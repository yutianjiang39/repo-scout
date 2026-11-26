from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        temporary = Path(f.name)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    return path


def write_json(path: Path, value: Any) -> Path:
    return atomic_write(
        path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )


def slug(value: str) -> str:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value
    ):
        raise ValueError(
            "Repository must be owner/name, without a URL or command options"
        )
    return value.lower()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid finding ID")
    return value


def safe_relative(value: str) -> str:
    p = Path(value)
    if (
        p.is_absolute()
        or ".." in p.parts
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise ValueError("File path must be relative and stay inside the repository")
    return p.as_posix()


def redact(text: str) -> str:
    # Never serialize credentials even when a child process echoes one.
    for key, value in os.environ.items():
        if (
            re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY", key, re.I)
            and len(value) >= 6
        ):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"']+", r"\1[REDACTED]", text
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b",
        "[REDACTED]",
        text,
    )
    return text
