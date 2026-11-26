# 交接记录

状态以程序为准，不在这里抄写：
- 审阅进度：`python orchestrate.py --db <任务目录>/review.sqlite status`（在 `projects/review-orchestrator/` 下）。
- 所有任务的文件与哈希：`records/records.sqlite`（`python -m ro.archive ... list`）。

进行中的任务（带 `review.sqlite` 账本）：
- PETL #564：`repos/petl/tasks/xlsm564-round/`（当前），`vba564-orchestrated/`（上一轮）。
- Coinbase：`repos/coinbase-advanced-py/tasks/coinbase-payment-method/`。
- pybotters：`repos/pybotters/tasks/pr595-784/`。

2026-11-26 之前的手工流程记录已归档：各库 `archives/legacy-tasks-20261126.zip`，旧交接全文在工作区 `notes/legacy-handoff-20261126.md`。其中的流程要求（xhigh、手工盲审步骤等）已作废，不再沿用。

这里只写程序记录不了的事：人做的决定、外部阻塞、下一步。每条一两行。
