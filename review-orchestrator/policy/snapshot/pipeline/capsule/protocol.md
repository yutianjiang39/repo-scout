# Your job: independent judgment

The system has already, for this gate only and in fresh copies:
- verified the frozen package, base commit and patch hash;
- run the focused tests on baseline + tests only, and on the full patch, plus the suite;
- generated mutants of the lines the patch adds and run the focused tests against each.
Raw logs and results: `REVIEW/evidence/evidence.json` (paths inside are relative to it).
The copies it used are in `.review-runs/` (`baseline-*`, `fixed-*`, `mutation`).

You decide what the evidence means. Do:
1. Root cause: is it real, reachable through a supported user action, and is this its cause?
   Read the source; do not just trust the requirement text.
2. Correctness: does the patch fix it completely? Edge cases, alternatives, side effects.
3. Compatibility: does it change behaviour that was previously correct?
4. Evidence: why does the baseline fail (target defect vs setup/import/network)? Is the
   suite meaningful? Anything the program did not cover?
5. Mutants: for every survivor, judge `equivalent`, `irrelevant` or `gap`.
6. Your own wrong implementation (mandatory, at least one, designed by you — e.g. a fix
   that only satisfies the example input): `harness.py probe-start <name>`, edit the copy,
   `harness.py probe-run <name>`. The system re-runs it in a fresh copy and compares with
   your claim. Do not edit test files in a probe.

You may run any other commands inside this workspace. You cannot change the official
candidate, and anything outside this workspace is off limits (reads outside it are
detected and void the review). Do not start other gates or publish anything.

Verdict: PASS (no unresolved substantive objection), REVISE (concrete problem + how to
reproduce + suggested fix), BLOCKED (cannot judge: say what is missing). Report format:
`REVIEW/contract.md`. Write it to `REVIEW/out/report.json` and print it last.
