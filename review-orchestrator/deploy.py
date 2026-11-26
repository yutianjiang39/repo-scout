"""Deploy/validate locally without changing the host repository or global config."""

import json
import shutil
import subprocess
import sys
from deployment import ROOT, PRIVATE, binary, environment
from gate import MODELS


def main():
    if not PRIVATE.is_file():
        sys.exit(
            "BLOCKED: private/credentials.json missing. Copy the complete private folder, not a Git clone."
        )
    if sys.version_info < (3, 10) or not shutil.which("git"):
        sys.exit("BLOCKED: install Python 3.10+ and Git")
    for role, model in MODELS.items():
        exe = binary(role)
        if not exe:
            sys.exit("BLOCKED: missing CLI for " + role)
        try:
            env = environment(role)
        except (ValueError, KeyError, TypeError):
            sys.exit("BLOCKED: invalid private route for " + role)
        if role == "claude":
            if not (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
                sys.exit("BLOCKED: Claude credential missing")
            args = [exe, "--help"]
            markers = ["--effort", "--model", "--resume", "--restricted", "--tools"]
        else:
            if role != "deepseek" and not env.get(
                "REVIEW_GPT_KEY" if role == "gpt" else "REVIEW_GEMINI_KEY"
            ):
                sys.exit("BLOCKED: credential missing for " + role)
            args = [exe, "models", "--standalone"]
            markers = [model.split("#", 1)[0]]
        result = subprocess.run(
            args, cwd=ROOT, env=env, capture_output=True, timeout=180
        )
        if result.returncode or not all(x.encode() in result.stdout for x in markers):
            sys.exit(
                "BLOCKED: CLI configuration/catalog check failed for "
                + role
                + "; do not print secrets"
            )
        print(role + ": CLI configuration OK", flush=True)
    subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        check=True,
    )
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "deployment.json").write_text(
        json.dumps(
            {
                "status": "local-ready",
                "models": MODELS,
                "online_review_verified": False,
            },
            indent=2,
        ),
        encoding="utf8",
    )
    print(
        "LOCAL READY: folder-scoped configuration and gates validated; no inference, votes, push or publication."
    )


if __name__ == "__main__":
    main()
