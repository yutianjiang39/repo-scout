"""Environment manager: shared dependency envs (built once) and disposable worktrees.

Runner interface: an env is {name, python, runner}. Only "venv"/"system" runners are
implemented; a container runner would implement the same `command()` contract.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

from .util import clone_base, git, need, rmtree, sha


class Env:
    def __init__(self, name, python, runner="venv", freeze=None):
        self.name, self.python, self.runner, self.freeze = name, python, runner, freeze

    def command(self, argv, dst):
        return [str(x).replace("{py}", self.python).replace("{work}", str(dst)) for x in argv]

    def child_env(self, dst):
        dst = Path(dst)
        env = os.environ.copy()
        paths = [str(dst)] + ([str(dst / "src")] if (dst / "src").is_dir() else [])
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env.update(PYTHONPATH=os.pathsep.join(paths), PYTHONDONTWRITEBYTECODE="1", REVIEW_ENV=self.name,
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return env

    def intact(self):
        return self.freeze is None or freeze_hash(self.python) == self.freeze

    def as_dict(self):
        return dict(python=self.python, runner=self.runner, freeze=self.freeze)


def venv_python(root):
    root = Path(root)
    for p in (root / "Scripts" / "python.exe", root / "bin" / "python"):
        if p.exists():
            return str(p)
    raise RuntimeError(f"no interpreter in {root}")


def freeze_hash(py):
    out = subprocess.run([py, "-m", "pip", "freeze", "--all"], capture_output=True, check=True).stdout
    return sha(out.replace(b"\r\n", b"\n"))


DEP_FILES = ("requirements", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock",
             "uv.lock", "constraints")


def dependency_key(repo, base, spec):
    """Cache key from dependency files only (path + blob hash) and the env spec.

    Bases that differ only in source code share one env; the package under test is never
    installed into it (reviewers' copies are put on PYTHONPATH), so sharing is safe.
    Files named in install commands count too, whatever their name.
    """
    named = {x for argv in spec.get("install", []) for x in argv if not x.startswith(("{", "-"))}
    entries = []
    for line in git(repo, "ls-tree", "-r", "--full-tree", base).decode(errors="replace").splitlines():
        meta, path = line.split("\t", 1)
        if any(k in Path(path).name for k in DEP_FILES) or path in named:
            entries.append(f"{path} {meta.split()[2]}")
    return sha(json.dumps(dict(deps=sorted(entries), spec=spec), sort_keys=True).encode())[:16]


def build(checks, repo, base, tree, cache, log=print):
    """Build (or reuse) one venv per env spec for this base. Returns {name: Env}."""
    spec_all = checks.get("envs") or {}
    if not spec_all:
        return {"system": Env("system", sys.executable, "system")}
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, spec in spec_all.items():
        key = dependency_key(repo, base, spec)
        d = cache / f"{name}-{key}"
        meta = d / "env.json"
        if meta.is_file():
            info = json.loads(meta.read_text(encoding="utf-8"))
            env = Env(name, info["python"], "venv", info["freeze"])
            if not env.intact():
                raise RuntimeError(f"cached env {d} was modified; delete it and rerun")
            log(f"env {name}: reuse {d}")
        else:
            rmtree(d)
            d.mkdir(parents=True)
            src = clone_base(d / "src", repo, base, tree)
            pip_env = dict(os.environ, PIP_CACHE_DIR=str(cache / "pip-cache"), PIP_DISABLE_PIP_VERSION_CHECK="1")
            with (d / "build.log").open("wb") as fh:
                subprocess.run([*spec.get("python", [sys.executable]), "-m", "venv", str(d / "venv")],
                               check=True, stdout=fh, stderr=subprocess.STDOUT)
                py = venv_python(d / "venv")
                for argv in spec.get("install", []):
                    argv = [x.replace("{py}", py).replace("{src}", str(src)) for x in argv]
                    fh.write(("$ " + subprocess.list2cmdline(argv) + "\n").encode())
                    fh.flush()
                    if subprocess.run(argv, cwd=src, env=pip_env, stdout=fh, stderr=subprocess.STDOUT).returncode:
                        raise RuntimeError(f"env {name}: install failed, see {d / 'build.log'}")
            rmtree(src)  # base sources must never shadow a reviewer's copy
            env = Env(name, py, "venv", freeze_hash(py))
            meta.write_text(json.dumps(dict(env.as_dict(), key=key, spec=spec, base=base), indent=2), encoding="utf-8")
            log(f"env {name}: built {d}")
        out[name] = env
    return out


def from_state(envs):
    return {k: Env(k, v["python"], v.get("runner", "venv"), v.get("freeze")) for k, v in envs.items()}


def worktree(work, name, diffs):
    """Disposable copy of the workspace HEAD with the given diff files applied."""
    work = Path(work)
    dst = work / ".review-runs" / name
    if dst.exists():
        subprocess.run(["git", "-C", str(work), "worktree", "remove", "--force", str(dst)], capture_output=True)
        rmtree(dst)
    git(work, "worktree", "prune")
    dst.parent.mkdir(exist_ok=True)
    git(work, "-c", "core.autocrlf=false", "worktree", "add", "-q", "--detach", str(dst), "HEAD")
    git(dst, "config", "core.autocrlf", "false")
    for d in diffs:
        git(dst, "apply", "--whitespace=nowarn", str(d))
    return dst


def drop_worktrees(work):
    work = Path(work)
    runs = work / ".review-runs"
    if runs.is_dir():
        for d in runs.iterdir():
            subprocess.run(["git", "-C", str(work), "worktree", "remove", "--force", str(d)], capture_output=True)
        rmtree(runs)
    subprocess.run(["git", "-C", str(work), "worktree", "prune"], capture_output=True)


def import_origin(env, module, dst):
    p = subprocess.run([env.python, "-c", f"import {module} as m; print(m.__file__)"], cwd=dst,
                       env=env.child_env(dst), capture_output=True)
    where = p.stdout.decode("utf-8", errors="replace").strip()
    ok = p.returncode == 0 and bool(where) and Path(where).resolve().is_relative_to(Path(dst).resolve())
    return dict(module=module, file=where or p.stderr.decode(errors="replace")[-500:], ok=ok)


def need_intact(envs):
    for e in envs.values():
        need(e.intact(), f"shared env '{e.name}' changed since it was built")
