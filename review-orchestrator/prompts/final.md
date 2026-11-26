Final round, in your own session. This directory holds every participating reviewer's
sealed first-round report (and any objection/recheck), each gate's program evidence
(`*-program-evidence.json`), the frozen candidate and `deliverable.md`.

Reconcile: disagreements between reports, judgments of mutants and probes, and every
material claim in deliverable.md against the program evidence. Re-check anything that is
insufficient. An unresolved substantive objection blocks release regardless of other votes.
Do not edit the official package.

Write the final report JSON (contract.md, with `body` = SHA-256 of deliverable.md and
`reconciliation`) to `report.json` in this directory and print it last.
