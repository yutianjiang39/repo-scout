from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .common import atomic_write, digest, identifier, now, redact, slug, write_json

GENERATED = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "target", "dist", "coverage", ".coverage"}


def minimal_env(home: Path | None = None) -> dict[str, str]:
    result = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8",
              "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0",
              "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    if home:
        result["HOME"] = str(home)
    if os.name == 'nt' and 'SystemRoot' in os.environ:
        result['SystemRoot'] = os.environ['SystemRoot']
    return result


def git(path: Path, *args: str, timeout: int = 120, input_text: str | None = None,
        extra_env: dict[str, str] | None = None) -> str:
    env = minimal_env()
    env.update(extra_env or {})
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never",
               "-c", "core.fsmonitor=false", "-C", str(path), *args]
    completed = subprocess.run(command, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, timeout=timeout, check=False)
    if completed.returncode:
        raise ValueError(redact(f"git {args[0]} failed: {completed.stderr.strip()[:1600]}"))
    return completed.stdout.removesuffix("\n")


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def snapshot(path: Path, base_sha: str | None = None) -> dict:
    current = head(path)
    base = base_sha or current
    if not re.fullmatch(r"[0-9a-f]{40,64}", base):
        raise ValueError("Invalid base commit SHA")
    raw = git(path, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    tracked = set(git(path, "ls-files", "-z").split("\0"))
    hashes = []
    for name in sorted(set(raw.split("\0"))):
        if not name or (name not in tracked and any(p in GENERATED for p in Path(name).parts)):
            continue
        p = path / name
        if p.is_symlink():
            hashes.append((name, "symlink:"+os.readlink(p)))
        elif p.is_file():
            hashes.append((name, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777))
        else:
            hashes.append((name, "deleted"))
    diff = git(path, "diff", "--no-ext-diff", "--no-textconv", "--binary", base, "--")
    changed = [x for x in git(path, "diff", "--no-ext-diff", "--name-only", "-z", base, "--").split("\0") if x]
    untracked = [n for n in raw.split("\0") if n and n not in tracked and not any(p in GENERATED for p in Path(n).parts)]
    return {"head_sha": current, "base_sha": base, "tree_hash": digest(hashes), "diff_hash": digest(diff),
            "diff": diff, "changed_files": sorted(set(changed+untracked)), "untracked": untracked,
            "clean": not git(path, "status", "--porcelain", "--untracked-files=no")}


def clone_repository(root: Path, repository: str, *, default_branch: str = "main", depth: int = 100) -> Path:
    repository = slug(repository)
    path = root / "repositories" / repository.replace("/", "__") / "original"
    remote = f"https://github.com/{repository}.git"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / ("clone-"+uuid.uuid4().hex[:10])
        try:
            git(path.parent, "clone", "--no-recurse-submodules", "--depth", str(max(1, depth)), "--", remote, str(temp), timeout=300)
            temp.rename(path)
        except BaseException:
            if temp.exists():
                shutil.rmtree(temp)
            raise
    else:
        if git(path, "remote", "get-url", "origin") != remote:
            raise ValueError("Original clone origin does not match the expected upstream")
        if git(path, "status", "--porcelain"):
            raise ValueError("Original clone contains local changes; refusing to overwrite it")
        # Explicit URL avoids configuration of an unexpected fetch destination.
        git(path, "fetch", "--no-tags", "--depth", str(max(1, depth)), remote, default_branch, timeout=300)
        git(path, "checkout", "--detach", "FETCH_HEAD")
    return path


def create_worktree(root: Path, original: Path, finding_id: str, base_sha: str, *, validation: bool = False) -> tuple[Path, str]:
    identifier(finding_id)
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        raise ValueError("Invalid worktree base SHA")
    bucket = "validation" if validation else "contributions"
    leaf = "repository-"+base_sha[:12] if validation else "repository"
    destination = (root / "workspace" / bucket / finding_id / leaf).resolve()
    branch = "contrib/" + finding_id.lower()
    if destination.exists():
        if validation and head(destination) != base_sha:
            raise ValueError("Validation workspace is stale; keep its evidence and create a new finding/preflight")
        if not validation and git(destination, "branch", "--show-current") != branch:
            raise ValueError("Existing worktree branch differs from the finding branch")
        return destination, branch
    destination.parent.mkdir(parents=True, exist_ok=True)
    if validation:
        git(original, "worktree", "add", "--detach", str(destination), base_sha)
    else:
        git(original, "worktree", "add", "-b", branch, str(destination), base_sha)
    return destination, branch


def run_command(store: Any, root: Path, finding: dict, command: list[str], *, purpose: str,
                reviewed: bool, runner: str = "docker", allow_host: bool = False,
                image: str = "python:3.11-slim", timeout: int = 300) -> dict:
    if not reviewed:
        raise ValueError("Inspect the command and referenced scripts/dependencies first; then pass --reviewed")
    if not command or any("\x00" in arg for arg in command):
        raise ValueError("An explicit argv command is required")
    if purpose not in {"baseline", "regression", "suite", "lint", "benchmark"}:
        raise ValueError("Unknown test purpose")
    workspace = finding.get("validation_workspace") if purpose == "baseline" else finding.get("workspace")
    if not workspace:
        raise ValueError("Create a validation/implementation worktree first")
    cwd = Path(workspace).resolve()
    before = snapshot(cwd, finding["base_sha"])
    if purpose == "baseline" and (before["head_sha"] != finding["base_sha"] or not before["clean"]):
        raise ValueError("Baseline must run against the unchanged upstream commit; add regression tests as new files")
    run_id = "RUN-"+uuid.uuid4().hex[:16]
    log_dir = root / "reports" / "tests" / run_id
    log_dir.mkdir(parents=True)
    home = log_dir / "home"
    home.mkdir()
    env = minimal_env(home)
    executed = list(command)
    if runner == "docker":
        if shutil.which("docker") is None:
            raise ValueError("Docker is unavailable. Install/use an isolated runner, or explicitly opt into reviewed host execution")
        executed = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
                    "--memory=1g", "--cpus=2", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "--mount", f"type=bind,source={cwd},target=/workspace", "-w", "/workspace", image, *command]
    elif runner != "host" or not allow_host:
        raise ValueError("Host execution requires runner=host and explicit --allow-host-execution")
    started = now()
    # Capture to bounded disk instead of unbounded subprocess.PIPE memory.
    timed_out = False
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(executed, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            return_code = proc.wait()
        output.seek(0)
        captured = output.read(2_000_001)
    truncated = len(captured) > 2_000_000
    output_text = redact(captured[:2_000_000].decode("utf-8", errors="replace")).replace("\r\n", "\n").replace("\r", "\n")
    after = snapshot(cwd, finding["base_sha"])
    record = {"run_id": run_id, "finding_id": finding["finding_id"], "purpose": purpose,
              "command": [redact(arg) for arg in command], "runner": runner, "reviewed": True, "started_at": started, "finished_at": now(),
              "source_sha": before["head_sha"], "tree_hash": before["tree_hash"], "after_tree_hash": after["tree_hash"],
              "source_unchanged": before["tree_hash"] == after["tree_hash"] and before["head_sha"] == after["head_sha"], "return_code": return_code,
              "timed_out": timed_out, "status": "TIMEOUT" if timed_out else ("PASS" if return_code == 0 else "FAIL"),
              "log_hash": digest(output_text), "log_path": str(log_dir / "output.log"), "truncated": truncated,
              "provenance": finding.get("provenance", "imported"), "captured_by": "repo-scout-runner"}
    atomic_write(log_dir / "output.log", output_text)
    write_json(log_dir / "run.json", record)
    store.put("test_run", run_id, record, reason="reviewed command actually executed", actor="runner")
    return record


def commit_changes(path: Path, *, message: str, name: str, email: str) -> str:
    if not message.strip() or not name.strip() or not email.strip():
        raise ValueError("Explicit commit message and author identity are required")
    git(path, "add", "--all")
    git(path, "-c", "user.name="+name, "-c", "user.email="+email, "commit", "--no-gpg-sign", "-m", message)
    return head(path)


def push_fork(path: Path, *, fork: str, branch: str, token: str, head_sha: str) -> None:
    slug(fork)
    if not token or not branch.startswith("contrib/") or not re.fullmatch(r"[A-Za-z0-9/_-]+", branch) or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha):
        raise ValueError("A write token and a dedicated contribution branch are required")
    # Credential supplied only through GIT_ASKPASS env, never URL, config, argv or log.
    with tempfile.TemporaryDirectory(prefix="repo-scout-auth-") as directory:
        askpass = Path(directory) / "askpass.py"
        askpass.write_text("#!/usr/bin/env python3\nimport os,sys\nprint('x-access-token' if 'username' in sys.argv[1].lower() else os.environ['SCOUT_PUSH_TOKEN'])\n")
        askpass.chmod(0o700)
        git(path, "-c", "credential.helper=", "push", "--porcelain", "--", f"https://github.com/{fork}.git",
            f"{head_sha}:refs/heads/{branch}", timeout=180,
            extra_env={"GIT_ASKPASS": str(askpass), "SCOUT_PUSH_TOKEN": token})
