import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parent.parent


class Refused(ValueError):
    """A gate rule refused the operation."""


def need(value, message):
    if not value:
        raise Refused(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha(Path(path).read_bytes())


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def git_text(repo, *args):
    return git(repo, *args).decode(errors="replace").strip()


def rmtree(path):
    def onerror(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if Path(path).exists():
        shutil.rmtree(path, onerror=onerror)


def diff_paths(diff_bytes):
    out = []
    for line in diff_bytes.decode(errors="replace").splitlines():
        if line.startswith("diff --git "):
            out.append(line.split(" b/", 1)[1] if " b/" in line else line.split()[-1])
    return out


def mirrors_dir():
    return Path(os.environ.get("REVIEW_MIRRORS", ROOT / ".cache" / "mirrors")).resolve()


def mirror(repo, base):
    """Per-repository bare mirror holding `base`; fetched once, reused by every gate and version."""
    repo = Path(repo).resolve()
    m = mirrors_dir() / (repo.name + "-" + sha(str(repo).encode())[:8] + ".git")
    if not m.is_dir():
        m.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_output(["git", "init", "-q", "--bare", str(m)], stderr=subprocess.PIPE)
    if subprocess.run(["git", "-C", str(m), "cat-file", "-e", base + "^{commit}"],
                      capture_output=True).returncode:
        git(m, "fetch", "-q", "--no-tags", repo.as_uri(), base)
    return m


def clone_base(dst, repo, base, tree=None):
    """Fresh checkout of one commit; LF-preserving; review dirs excluded.

    Objects come from a shared local mirror (alternates, nothing copied); the checkout itself is
    new and private to this workspace, so isolation is unchanged.
    """
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    git(dst, "init", "-q")
    git(dst, "config", "core.autocrlf", "false")
    with open(dst / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
        fh.write("\n/REVIEW/\n/.review-runs/\n")
    src = mirror(repo, base)
    (dst / ".git" / "objects" / "info" / "alternates").write_text(str(src / "objects") + "\n", encoding="utf-8")
    git(dst, "fetch", "-q", "--no-tags", src.as_uri(), base)
    git(dst, "checkout", "-q", "--detach", "FETCH_HEAD")
    if tree:
        need(git_text(dst, "rev-parse", "HEAD^{tree}") == tree, "Wrong tree in fresh checkout")
    return dst
