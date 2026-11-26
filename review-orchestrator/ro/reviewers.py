"""Independent reviewer runtime: CLI adapters + raw output capture.

Adding a reviewer = an entry in config/reviewers.json; a new CLI = one adapter here.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from . import config

PROMPT_FIRST = "Read REVIEW/STAGE.md and follow it. Return the full report as a JSON object."
PROMPT_FINAL = "Read {folder}/final.md and the materials in that directory. Return the full report as a JSON object."


def _environment(role):
    try:
        from .deployment import PRIVATE, environment  # folder-scoped private wiring, optional
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
               "--allowedTools", self.TOOLS,
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


STALL = int(os.environ.get("REVIEW_STALL", "600"))  # seconds without model progress -> abort
HEARTBEAT = 60
PATH_KEYS = ("file_path", "filePath", "path", "pattern", "command", "url")


def _brief(value, n=90):
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool(name, args):
    args = args if isinstance(args, dict) else {}
    arg = next((args[k] for k in PATH_KEYS if args.get(k)), "")
    return f"tool {name} {_brief(arg)}".rstrip()


def describe(obj):
    """One progress line per CLI event, or None. Returns (text, is_progress)."""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "api_retry":
        why = obj.get("error_status") or obj.get("error") or "?"
        return f"API retry {obj.get('attempt')}/{obj.get('max_retries')} ({why})", False
    if t == "retry":  # opencode
        err = obj.get("error") or {}
        return f"API retry {obj.get('attempt')} ({_brief(err.get('message') or err.get('type'), 60)})", False
    if t == "system" and obj.get("subtype") == "init":
        return f"session {obj.get('session_id')} started, waiting for model", False
    if t == "assistant":  # claude stream-json
        out = []
        for c in (obj.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                out.append(_tool(c.get("name"), c.get("input")))
            elif c.get("type") == "text" and c.get("text", "").strip():
                out.append("says: " + _brief(c["text"]))
            elif c.get("type") == "thinking":
                out.append("thinking")
        return ("; ".join(out) or "model step"), True
    if t == "user":
        return None, True  # tool results: progress, no line
    if t == "result":
        cost, bits = obj.get("total_cost_usd"), []
        if obj.get("subtype"):
            bits.append(str(obj["subtype"]))
        if obj.get("num_turns"):
            bits.append(f"{obj['num_turns']} turns")
        if isinstance(cost, (int, float)):
            bits.append(f"${cost:.2f}")
        return "model finished" + (f": {', '.join(bits)}" if bits else ""), True
    part = obj.get("part") or {}
    if t == "tool_use":  # opencode
        return _tool(part.get("tool"), (part.get("state") or {}).get("input")), True
    if t == "text" and part.get("text", "").strip():
        return "says: " + _brief(part["text"]), True
    if t in ("step_start", "step_finish", "reasoning"):
        return None, True
    if t == "error":
        return "error: " + _brief(obj.get("error") or obj), False
    return None, False


def _kill(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.kill()


def run_cli(role, work, transcript_dir, final=False, session=None, folder=None, timeout=None, prompt=None,
            log=print, stall=None):
    """final=True resumes the reviewer's own session (final round or recheck).

    Streams one line per reviewer step to `log`; aborts (exit 124) on `timeout` total seconds or
    `stall` seconds without model progress (API retries do not count as progress)."""
    cfg = config.reviewers()[role]
    adapter = ADAPTERS[cfg["adapter"]]
    exe = adapter.binary()
    if not exe:
        raise RuntimeError(f"CLI not installed for {role} ({cfg['adapter']})")
    prompt = prompt or (PROMPT_FINAL.format(folder=folder) if final else PROMPT_FIRST)
    cmd, fixed_session = adapter.command(exe, cfg, prompt, session, final)
    transcript_dir = Path(transcript_dir)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    stall = STALL if stall is None else stall
    t0 = time.monotonic()
    mark = dict(progress=t0, event=t0, last="started", steps=0)

    def say(msg):
        e = int(time.monotonic() - t0)
        log(f"[{role}] {e // 60:02d}:{e % 60:02d} {msg}")

    def pump(stream, out):
        for line in iter(stream.readline, b""):
            out.write(line)
            out.flush()
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            text, progress = describe(obj)
            now = time.monotonic()
            mark["event"] = now
            if progress:
                mark["progress"] = now
                mark["steps"] += 1
            if text:
                mark["last"] = text
                say(text)

    reason = None
    with (transcript_dir / "stdout").open("wb") as out, (transcript_dir / "stderr").open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=work, env=_environment(role), stdout=subprocess.PIPE, stderr=err)
        reader = threading.Thread(target=pump, args=(proc.stdout, out), daemon=True)
        reader.start()
        beat = t0
        while proc.poll() is None:
            time.sleep(1)
            now = time.monotonic()
            if timeout and now - t0 > timeout:
                reason = f"timeout after {timeout}s"
            elif stall and now - mark["progress"] > stall:
                reason = f"no model progress for {stall}s (last: {mark['last']})"
            if reason:
                say(f"ABORT: {reason}")
                _kill(proc)
                break
            if now - max(mark["event"], beat) >= HEARTBEAT:
                beat = now
                say(f"... still running, {mark['steps']} steps, quiet {int(now - mark['event'])}s "
                    f"(last: {mark['last']})")
        proc.wait()
        reader.join(timeout=10)
    code = 124 if reason else proc.returncode
    if not reason:
        say(f"CLI exited {code}")
    raw = (transcript_dir / "stdout").read_text(encoding="utf-8", errors="replace")
    return dict(exit=code, session=fixed_session or session or find_session(raw), raw=raw,
                model=cfg["model"], transcript=transcript_dir, abort=reason)
