"""Private, folder-scoped runtime wiring. Never print credentials."""

import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private" / "credentials.json"


def environment(role):
    secrets = json.loads(PRIVATE.read_text(encoding="utf-8"))
    env = os.environ.copy()
    if role == "claude":
        for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(secrets["claude"])
        return env
    cfg = json.loads(
        (ROOT / "config/providers.example.json").read_text(encoding="utf-8")
    )
    if role == "deepseek":
        cfg["providers"] = {}
    else:
        slot = "vvvvvip" if role == "gpt" else "review-gemini"
        provider = cfg["providers"][slot]
        cfg["providers"] = {slot: provider}
        route = secrets[role]
        env[provider["env"][0]] = route["key"]
        provider["settings"].update(
            baseURL=route["baseURL"], apiKey="{env:" + provider["env"][0] + "}"
        )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    return env


def binary(role):
    if role == "claude":
        return shutil.which(os.environ.get("REVIEW_CLAUDE_BIN", "claude"))
    return (
        shutil.which(os.environ["REVIEW_OPENCODE_BIN"])
        if "REVIEW_OPENCODE_BIN" in os.environ
        else shutil.which("opencode2") or shutil.which("opencode")
    )
