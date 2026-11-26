"""Read-only repository preparation; generated copies retain exact source bytes.

No repository code is imported or executed. All paths in the manifest refer to
the original repository; the generated ``files/`` tree keeps that same layout.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any


EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "target",
    "venv",
    ".venv",
    "env",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "coverage",
    ".next",
    "vendor",
    "third_party",
    ".tox",
    ".idea",
    ".vscode",
}
SENSITIVE_NAMES = {
    ".mcp.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".mp3",
    ".mp4",
    ".wav",
    ".woff",
    ".woff2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pyc",
    ".sqlite",
    ".db",
    ".bin",
}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[\w./+=~-]{8,}"),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@:]+:[^\s/@]+@"
    ),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)"
        r"['\"]?\s*[:=]\s*['\"]([^'\"\r\n]{4,})['\"]"
    ),
    re.compile(
        r"(?im)^\s*(?:aws_secret_access_key|password|api[_-]?key|access[_-]?token)\s*[:=]\s*(?!os\.environ\b|os\.getenv\b)([^\s'\"#]{4,})"
    ),
]


def secret_reason(content: str) -> str | None:
    """Conservative local screen. Return a category, never the secret value."""
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(content):
            sample = match.group(1) if match.lastindex else match.group(0)
            # Explicit documentation placeholders are not credentials.
            if sample.lower() in {"example", "placeholder", "your_api_key", "changeme"}:
                continue
            return "possible_hardcoded_secret"
    return None


def token_estimate(text: str) -> int:
    """UTF-8 heuristic for chunk sizing, not a provider tokenizer/billing count."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def _hash(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Generated output must not contain symlinks")
    data = content.encode("utf-8") if isinstance(content, str) else content
    if not path.exists() or path.read_bytes() != data:
        temp = path.with_name(path.name + ".tmp")
        if temp.is_symlink():
            raise ValueError("Generated temporary file is a symlink")
        temp.write_bytes(data)
        temp.replace(path)


def _json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _imports(tree: ast.AST | None) -> list[str]:
    if tree is None:
        return []
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return sorted(result)


def chunk_source(
    repository: str, path: str, content: str, *, chunk_tokens: int = 4000
) -> list[dict]:
    if chunk_tokens < 64:
        raise ValueError("chunk_tokens must be at least 64")
    lines = content.splitlines(keepends=True)
    if not lines:
        return []
    tree = None
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            pass
    boundaries = {1, len(lines) + 1}
    if tree is not None:
        for node in tree.body:
            start = min(
                [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
            )
            boundaries.add(start)
            boundaries.add(getattr(node, "end_lineno", node.lineno) + 1)
    else:
        # Blank lines are a safe fallback boundary for non-Python source.
        boundaries.update(i + 2 for i, line in enumerate(lines) if not line.strip())
    ordered = sorted(boundaries)
    atoms = [(a, b - 1) for a, b in zip(ordered, ordered[1:]) if a < b]
    ranges: list[tuple[int, int, bool]] = []
    start = end = None
    for left, right in atoms:
        atom_tokens = token_estimate("".join(lines[left - 1 : right]))
        if atom_tokens > chunk_tokens:
            if start is not None:
                ranges.append((start, end, False))
                start = end = None
            split_start = left
            for line_no in range(left, right + 1):
                if (
                    line_no > split_start
                    and token_estimate("".join(lines[split_start - 1 : line_no]))
                    > chunk_tokens
                ):
                    ranges.append((split_start, line_no - 1, True))
                    split_start = line_no
            ranges.append((split_start, right, True))
        elif start is None:
            start, end = left, right
        elif token_estimate("".join(lines[start - 1 : right])) <= chunk_tokens:
            end = right
        else:
            ranges.append((start, end, False))
            start, end = left, right
    if start is not None:
        ranges.append((start, end, False))
    chunks = []
    imports = _imports(tree)
    for start, end, partial in ranges:
        text = "".join(lines[start - 1 : end])
        digest = _hash(text)
        chunks.append(
            {
                "chunk_id": _hash(f"{repository}:{path}:{start}:{end}:{digest}")[:24],
                "repository": repository,
                "file_path": path,
                "line_start": start,
                "line_end": end,
                "content_hash": digest,
                "content": text,
                "dependencies": imports,
                "token_estimate": token_estimate(text),
                "partial": partial,
                "split_reason": "oversized_statement_or_function" if partial else None,
            }
        )
    return chunks


def prepare_bundle(
    repository_path: Path,
    output: Path,
    *,
    repository: str = "",
    max_file_bytes: int = 500_000,
    chunk_tokens: int = 4000,
) -> dict:
    root = Path(repository_path).resolve(strict=True)
    output = Path(output).absolute()
    if not root.is_dir():
        raise ValueError("Repository path must be a directory")
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise ValueError("Bundle output must not use symlinks")
    output = output.resolve()
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Bundle and original repository must be disjoint directories")
    if max_file_bytes < 1:
        raise ValueError("max_file_bytes must be positive")
    output.mkdir(parents=True, exist_ok=True)
    previous = {}
    if (output / "manifest.json").exists():
        previous = json.loads((output / "manifest.json").read_text("utf-8"))
    included, excluded, chunks, dependencies = [], [], [], {}
    python_symbols = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(directory)
        for name in sorted(dirnames[:]):
            p = directory / name
            hidden = name.startswith(".") and name.casefold() != ".github"
            if p.is_symlink() or hidden or name.casefold() in EXCLUDED_DIRS:
                dirnames.remove(name)
                excluded.append(
                    {
                        "path": p.relative_to(root).as_posix() + "/",
                        "reason": "symlink"
                        if p.is_symlink()
                        else "private_configuration"
                        if hidden and name not in EXCLUDED_DIRS
                        else "generated_or_vendor_directory",
                    }
                )
        for name in sorted(filenames):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            reason = None
            if path.is_symlink():
                reason = "symlink"
            elif (
                name.lower().startswith(".env")
                or name.lower() in SENSITIVE_NAMES
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
            ):
                reason = "sensitive_filename"
            elif name.startswith(".") or name.casefold() in {
                "claude.md",
                "agents.md",
                "gemini.md",
                "mcp.json",
            }:
                reason = "private_configuration"
            elif path.suffix.lower() in BINARY_SUFFIXES:
                reason = "binary_or_media"
            elif name.endswith((".min.js", ".min.css", ".map")):
                reason = "minified_or_generated"
            elif path.stat().st_size > max_file_bytes:
                reason = "file_size_limit"
            if reason:
                excluded.append({"path": relative, "reason": reason})
                continue
            try:
                raw = path.read_bytes()
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                excluded.append({"path": relative, "reason": "unreadable_or_non_utf8"})
                continue
            reason = "binary_content" if "\x00" in content else secret_reason(content)
            if reason:
                excluded.append({"path": relative, "reason": reason})
                continue
            destination = output / "files" / relative
            _write(destination, raw)
            file_chunks = chunk_source(
                repository, relative, content, chunk_tokens=chunk_tokens
            )
            chunks.extend(file_chunks)
            included.append(
                {
                    "path": relative,
                    "bundle_path": "files/" + relative,
                    "sha256": _hash(raw),
                    "bytes": len(raw),
                    "lines": len(content.splitlines()),
                    "chunks": [c["chunk_id"] for c in file_chunks],
                }
            )
            if path.suffix == ".py":
                try:
                    tree = ast.parse(content)
                    dependencies[relative] = _imports(tree)
                    python_symbols[relative] = [
                        {"name": n.name, "line": n.lineno, "kind": type(n).__name__}
                        for n in tree.body
                        if isinstance(
                            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                        )
                    ]
                except (SyntaxError, ValueError):
                    dependencies[relative] = {"status": "parse_failed"}
            if name in {"pyproject.toml", "Cargo.toml"}:
                try:
                    parsed = tomllib.loads(content)
                    dependencies[relative] = {
                        k: parsed[k]
                        for k in (
                            "project",
                            "dependencies",
                            "dev-dependencies",
                            "workspace",
                        )
                        if k in parsed
                    }
                except tomllib.TOMLDecodeError:
                    dependencies[relative] = {"status": "parse_failed"}
    # Remove only files previously owned by this generated bundle, never source files.
    kept = {f["path"] for f in included}
    for item in previous.get("files", []):
        relative = item.get("path", "")
        candidate = output / "files" / relative
        if (
            relative
            and relative not in kept
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts
            and not candidate.is_symlink()
            and candidate.resolve().is_relative_to(output / "files")
            and candidate.is_file()
        ):
            candidate.unlink()
    head_sha = _git(root, "rev-parse", "HEAD") or None
    history = _git(root, "log", "-15", "--format=%h %s")
    # Commit messages are also untrusted and may contain credentials.
    if secret_reason(history):
        history = "[History omitted: possible secret detected]"
    manifest = {
        "schema_version": 1,
        "repository": repository,
        "commit_sha": head_sha,
        "files": included,
        "excluded": excluded,
        "chunk_count": len(chunks),
        "content_hash": _hash(json.dumps([(f["path"], f["sha256"]) for f in included])),
        "limitations": [
            "Static import/symbol map, not a resolved call graph",
            "Secret screen is heuristic; review bundle before uploading",
            "Excluded directory entries cover their entire subtree",
        ],
    }
    map_lines = [
        f"# Repository map: {repository or root.name}",
        "",
        f"Commit: `{head_sha or 'unknown (not a Git checkout)'}`",
        "",
        "Static file/import inventory; no repository code was executed.",
        "",
        "## Files",
        "",
    ]
    for item in included:
        map_lines.append(f"- `{item['path']}` — {item['lines']} lines")
    map_lines += ["", "## Python entry points and definitions", ""]
    for path, symbols in python_symbols.items():
        for symbol in symbols:
            map_lines.append(
                f"- `{path}:{symbol['line']}` — {symbol['kind']} `{symbol['name']}`"
            )
    _json(output / "manifest.json", manifest)
    _json(output / "chunks.json", chunks)
    _json(output / "dependency_summary.json", dependencies)
    _write(output / "repo_map.md", "\n".join(map_lines) + "\n")
    _write(
        output / "recent_history.md",
        "# Recent Git history\n\n" + (history or "Unavailable") + "\n",
    )
    return manifest
