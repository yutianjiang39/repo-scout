"""Independent reviewer runtime: CLI adapters + raw output capture.

Adding a reviewer = an entry in config/reviewers.json; a new CLI = one adapter here.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from . import config

PROMPT_FIRST = "Read REVIEW/STAGE.md and follow it. Return the full report as a JSON object."
PROMPT_FINAL = "Read {folder}/final.md and the materials in that directory. Return the full report as a JSON object."


def _environment(role):
    try:
        from deployment import PRIVATE, environment  # folder-scoped private wiring, optional
    except ImportError:
        return os.environ.copy()
    return environment(role) if PRIVATE.is_file() else os.environ.copy()


class OpenCode:
    def binary(self):
        if "REVIEW_OPENCODE_BIN" in os.environ:
            return shutil.which(os.environ["REVIEW_OPENCODE_BIN"])
        return shutil.which("opencode2") or shutil.which("opencode")

    def command(self, exe, cfg, prompt, session, final):
        cmd = [exe, "run", "--standalone", "--model", cfg["model"], "--format", "json"]
        if final:
            cmd += ["--session", session]
        return cmd + [prompt], None


class Claude:
    TOOLS = "Read,Glob,Grep,Bash,Write,Edit"

    def binary(self):
        return shutil.which(os.environ.get("REVIEW_CLAUDE_BIN", "claude"))

    def command(self, exe, cfg, prompt, session, final):
        session = session or str(uuid.uuid4())
        cmd = [exe, "-p", prompt, "--model", cfg["model"], "--effort", cfg.get("effort", "medium"),
               "--output-format", "stream-json", "--verbose",
               # --restricted confines file tools to the working directory and ignores
               # user/project settings; Bash is re-enabled explicitly via --tools.
               "--restricted", "--tools", self.TOOLS, "--allowedTools", self.TOOLS,
               "--strict-mcp-config", "--resume" if final else "--session-id", session]
        cmd += cfg.get("extra_args", [])
        return cmd, session


ADAPTERS = {"opencode": OpenCode(), "claude": Claude()}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for x in value.values():
            yield from strings(x)
    elif isinstance(value, list):
        for x in value:
            yield from strings(x)


def find_session(text):
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        stack = [obj]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                for k in ("session_id", "sessionID", "sessionId"):
                    if isinstance(o.get(k), str) and o[k].strip():
                        return o[k]
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return None


def find_report(text):
    found, decoder = None, json.JSONDecoder()
    sources = [text]
    for line in text.splitlines():
        try:
            sources.extend(strings(json.loads(line)))
        except ValueError:
            pass
    for src in sources:
        i = src.find("{")
        while i != -1:
            try:
                obj, end = decoder.raw_decode(src, i)
            except ValueError:
                i = src.find("{", i + 1)
                continue
            if isinstance(obj, dict) and {"role", "verdict"} <= set(obj):
                found = obj
            i = src.find("{", end)
    return found


def run_cli(role, work, transcript_dir, final=False, session=None, folder=None, timeout=None, prompt=None):
    """final=True resumes the reviewer's own session (final round or recheck)."""
    cfg = config.reviewers()[role]
    adapter = ADAPTERS[cfg["adapter"]]
    exe = adapter.binary()
    if not exe:
        raise RuntimeError(f"CLI not installed for {role} ({cfg['adapter']})")
    prompt = prompt or (PROMPT_FINAL.format(folder=folder) if final else PROMPT_FIRST)
    cmd, fixed_session = adapter.command(exe, cfg, prompt, session, final)
    transcript_dir = Path(transcript_dir)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    with (transcript_dir / "stdout").open("wb") as out, (transcript_dir / "stderr").open("wb") as err:
        try:
            code = subprocess.run(cmd, cwd=work, env=_environment(role), stdout=out, stderr=err,
                                  timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = 124
    raw = (transcript_dir / "stdout").read_text(encoding="utf-8", errors="replace")
    return dict(exit=code, session=fixed_session or session or find_session(raw), raw=raw,
                model=cfg["model"], transcript=transcript_dir)
