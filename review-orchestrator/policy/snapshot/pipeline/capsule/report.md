Return one JSON object (UTF-8). Do not include credentials. Do not report exit codes or
test counts: the system measured those itself and checks your claims against them.

Always: `role`, `attempt`, `patch`, `verdict` (PASS | REVISE | BLOCKED), `summary`,
`objections` (array; empty for PASS; for REVISE each item states the problem, how to
reproduce it and a suggested fix).

Code review, first round — judgment fields (non-empty text for PASS):
- `root_cause`, `correctness`, `compatibility`, `test_adequacy`, `risks`
- `evidence_review`: what you checked in REVIEW/evidence and what it shows
- `baseline_failure`: `target-defect` | `none` | `setup` (why the focused tests behave as
  they do on baseline; `setup` means the evidence cannot show the defect)
- `mutants`: one entry per SURVIVING mutant: `{"id", "judgment": "equivalent"|"irrelevant"|"gap", "reason"}`
- `probes`: your wrong implementations: `{"name", "rationale", "claimed": "detected"|"missed"|"invalid"}`;
  for a `missed` probe also `judgment` (`equivalent`|`irrelevant`|`gap`) and `reason`.
A judged `gap` (mutant or probe) is a substantive objection: use REVISE.

Artifact review, first round PASS: `checks` (one object per criterion with non-empty
`criterion`, `method`, `observed`) and `probe` (counterexample tried and result).

Final round: also `body` (SHA-256 of deliverable.md) and `reconciliation` (which reports,
disagreements, program evidence and delivery claims you checked).
