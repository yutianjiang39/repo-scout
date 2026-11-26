"""Reviewer registry. Order of entries is the gate order."""

import json
import os
from pathlib import Path

from .util import ROOT, need

PATH = Path(os.environ.get("REVIEW_REVIEWERS", ROOT / "config" / "reviewers.json"))


def reviewers():
    data = json.loads(PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def roles():
    return tuple(reviewers())


def required():
    return tuple(k for k, v in reviewers().items() if v.get("required"))


def model(role):
    return reviewers()[role]["model"]


def parse_gates(text):
    order = roles()
    gates = [g.strip() for g in text.split(",") if g.strip()] if text else list(order)
    need(set(gates) <= set(order) and len(set(gates)) == len(gates), "Unknown gate in --gates")
    need(set(required()) <= set(gates), f"Required gates {required()} must be included")
    return [r for r in order if r in gates]
