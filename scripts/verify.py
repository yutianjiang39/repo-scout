"""Run credential-free tests and write reproducible verification artifacts."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from repo_scout.common import redact  # noqa: E402


def main() -> int:
    directory = ROOT / "validation"
    directory.mkdir(exist_ok=True)
    started = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    timer = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with (directory / "test-output.txt").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    log = directory / "test-output.txt"
    log.write_text(redact(log.read_text()), encoding="utf-8")
    files = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for directory_name in ("src", "tests") for p in sorted((ROOT / directory_name).rglob("*.py"))}
    report = {"version": "0.1.0", "started_at": started, "timezone": "Asia/Shanghai",
              "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "duration_seconds": round(time.monotonic()-timer, 3), "python": sys.version.split()[0],
              "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "successful": result.wasSuccessful(), "source_sha256": files,
              "evidence": {"unit_and_integration_tests": "actual execution", "git_worktrees": "actual local execution",
                           "baseline_and_fixed_tests": "actual local execution", "cc_reports": "mocked fixtures",
                           "github_api": "mocked fixtures", "model_api": "httpx.MockTransport"},
              "live_validation": {"github_rest": False, "model_providers": False, "claude_code_cli": False,
                                  "docker_runner": False, "public_writes": False},
              "note": "Tests validate behavior; no synthetic or mocked result constitutes a real contribution."}
    (directory / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"source_sha256", "evidence", "live_validation"}}, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
