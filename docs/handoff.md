# 交接记录

状态以程序为准，不在这里抄写：
- 审阅进度：`python orchestrate.py --db <任务目录>/review.sqlite status`（在 `projects/repo-scout/review-orchestrator/` 下）。
- 所有任务的文件与哈希：`records/records.sqlite`（`python -m ro.archive ... list`）。

进行中的任务（带 `review.sqlite` 账本）：
- PETL #564：`repos/petl/tasks/xlsm564-round/`（当前），`vba564-orchestrated/`（上一轮）。
- Coinbase：`repos/coinbase-advanced-py/tasks/coinbase-payment-method/`；`issue94/`（v3 双 PASS，phase=ready，未发布）。
- pybotters：`repos/pybotters/tasks/pr595-784/`。

2026-11-26 之前的手工流程记录已归档：各库 `archives/legacy-tasks-20261126.zip`，旧交接全文在工作区 `notes/legacy-handoff-20261126.md`。其中的流程要求（xhigh、手工盲审步骤等）已作废，不再沿用。

这里只写程序记录不了的事：人做的决定、外部阻塞、下一步。每条一两行。

- 2026-11-26 用户决定：审阅流水线只保留一份，即 `projects/repo-scout/review-orchestrator/`（受 git 管理）。原来的 `projects/review-orchestrator/` 副本已删除，`private/`、`.runtime/`、`.cache/` 一起搬过来了。改完代码直接在 repo-scout 里提交推送，不再维护第二份。
- 2026-11-26 分支约定：repo-scout 的主检出一直停在 `main`。功能开发用单独的 worktree 加短期分支，合入 main 后，分支和 worktree 一起删掉。
- 2026-11-27 issue94 v1（evidence `v1-gpt-b6c49f15`）的两项 BLOCKED 都不是候选的问题：py312 卡在上游测试 `test_reconnect_fail` 的时序竞争上（基线和候选各连跑 20 次，全部通过）；py314 是 Claude 的排查实验在同一时间占用了 8765 端口（WinError 10048）。coinbase 的 websocket 测试固定使用 8765 端口，同一台机器上不要并行跑两个套件。
