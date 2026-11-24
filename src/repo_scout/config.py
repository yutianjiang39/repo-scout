from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

DEFAULTS = {
    "username": "yutianjiang39",
    "profile_repository": "yutianjiang39/yutianjiang39",
    "github": {"read_token_env": "GITHUB_READ_TOKEN", "write_token_env": "GITHUB_WRITE_TOKEN"},
    "search": {"limit": 25, "categories": [
        {"name": "Hyperliquid Ecosystem", "query": "hyperliquid archived:false fork:false", "weight": 40},
        {"name": "Independent Trading Infrastructure", "query": "trading websocket language:Python archived:false fork:false", "weight": 28},
        {"name": "Quantitative & Data Analytics", "query": "backtesting language:Python archived:false fork:false", "weight": 20},
        {"name": "Rust / Python Engineering Tools", "query": "async language:Rust archived:false fork:false", "weight": 12},
    ]},
    "screening": {"max_inactive_days": 180, "allow_unknown_license": False, 'maximum_size_kb':50000,
                  'preferred_size_kb':{'min':100,'max':10000},'preferred_stars':{'min':50,'max':1000},
                  'preferred_forks':{'min':5,'max':200},'preferred_owner_types':['User']},
    "scoring": {"threshold": 70},
    "providers": {},
    "roles": {},
    "audit": {"max_findings": 5, "input_token_budget": 100000, "output_token_budget": 40000, "concurrency": 2,
              "prompt_version": "1", "cost_budget_usd": None, "max_chunks": 80, "context_tokens": 1500},
    "validation": {"preflight_max_age_hours": 24},
    "execution": {"runner": "docker", "image": "python:3.11-slim", "timeout_seconds": 300},
    "tracking": {"interval_hours": 24, "trusted_maintainers": {}},
}


def merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def load_config(path: Path | None) -> dict:
    data = yaml.safe_load(path.read_text()) if path and path.exists() else {}
    if data is not None and not isinstance(data, dict):
        raise ValueError("Configuration must be a YAML mapping")
    out = merge(DEFAULTS, data or {})
    if out["tracking"]["interval_hours"] < 1:
        raise ValueError("Tracking interval must be at least one hour")
    return out


def write_default(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(DEFAULTS, allow_unicode=True, sort_keys=False))
