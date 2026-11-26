"""Scripted stand-in for a reviewer CLI (tests only). FAKE_MODE selects behaviour."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "pass")
work = Path.cwd()
session = next((args[i + 1] for i, a in enumerate(args) if a in ("--session-id", "--resume", "--session")), None)
session = session or "ses_" + uuid.uuid4().hex[:10]
prompt = args[args.index("-p") + 1] if "-p" in args else args[-1]
binding = json.loads((work / "REVIEW/binding.json").read_text())
role, model = binding["role"], binding["model"]
base_model = model.split("#")[0]
if mode == "wrong-model":
    receipt = {"modelUsage": {"some-other-model": {}}}
elif "/" in base_model:
    receipt = {"providerID": base_model.split("/")[0], "modelID": base_model.split("/")[1]}
else:
    receipt = {"modelUsage": {base_model: {}}}
print(json.dumps(dict(type="system", session_id=session, sessionID=session, **receipt)))
if mode == "peek":
    print(json.dumps(dict(type="tool", input=os.environ["FAKE_PEEK"])))


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


if "final.md" in prompt:
    folder = work / prompt.split("Read ")[1].split("/final.md")[0]
    body = hashlib.sha256((folder / "deliverable.md").read_bytes()).hexdigest()
    write(folder / "report.json", dict(role=role, attempt=binding["attempt"], patch=binding["patch"], body=body,
                                       verdict="PASS", summary="final ok", objections=[],
                                       reconciliation="read all first reports and program evidence"))
elif "counter-evidence" in prompt:
    rep = json.loads((work / "REVIEW/out/last.json").read_text())
    rep.update(verdict="PASS", objections=[], reconciliation="counter-evidence convincing; withdrawn")
    write(work / "REVIEW/out/report.json", rep)
else:
    ev = json.loads((work / "REVIEW/evidence/evidence.json").read_text())
    h = [sys.executable, "-B", "REVIEW/harness.py"]
    subprocess.run(h + ["probe-start", "example-only"], check=True, capture_output=True)
    (work / ".review-runs/probe-example-only/value.py").write_text(
        "def value(x):\n    return 2 if x == -2 else x\n")
    subprocess.run(h + ["probe-run", "example-only"], capture_output=True)
    survivors = ev.get("mutation", {}).get("survived", [])
    verdict = "REVISE" if mode in ("revise", "revise-then-withdraw") else "PASS"
    rep = dict(role=role, attempt=binding["attempt"], patch=binding["patch"], verdict=verdict,
               summary="independent review", objections=["zero not covered"] if verdict == "REVISE" else [],
               root_cause="negative input returned unchanged", correctness="abs semantics restored",
               compatibility="non-negative inputs unchanged", test_adequacy="covers sign change",
               risks="low", evidence_review="baseline fails on value(-2); fixed green",
               baseline_failure="target-defect",
               mutants=[dict(id=m, judgment="gap" if mode == "gap" else "equivalent", reason="x<=0 same for 0")
                        for m in survivors],
               probes=[dict(name="example-only", rationale="only satisfies the example input",
                            claimed="missed" if mode == "lie" else "detected")])
    write(work / "REVIEW/out/report.json", rep)
    write(work / "REVIEW/out/last.json", rep)
print(json.dumps(dict(type="result", session_id=session, result="done")))
