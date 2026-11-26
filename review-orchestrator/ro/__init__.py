"""Review Orchestrator.

Program owns the workflow (freeze, environments, test runs, mutation, evidence,
promotion). Models own judgment (root cause, correctness, adequacy, verdict).

Modules
  util       hashing, git, small helpers
  config     reviewer registry (config/reviewers.json)
  ledger     append-only SQLite state + immutable file store
  artifacts  freeze candidates, build the review capsule and manifest
  envs       shared dependency envs, worktrees, child process env
  engine     deterministic test runs, junit parsing, probe re-verification
  mutation   AST mutants of changed lines
  evidence   evidence bundles, sealing, transcript audit, model receipts
  reviewers  CLI adapters (opencode, claude), report/session extraction
  gates      gate controller: state machine and report validation
  orchestrator  lifecycle: start / run / final / status
"""
