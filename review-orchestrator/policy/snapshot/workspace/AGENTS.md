# 开源贡献工作区

## 目录

- `projects/review-orchestrator/`：审阅流水线。审阅一律通过 `orchestrate.py` / `gate.py`，规则由程序执行，读它的 README 即可。模型地址与密钥在其 `private/credentials.json`（不入库，不展示）。
- `projects/repo-scout/`：巡检项目。交接见 `docs/handoff.md`。
- `repos/<仓库>/`：`work/` 实施副本，`tasks/<任务>/` 任务材料（每个任务一个 `review.sqlite`），`tmp/` 临时工作区，`archives/` 归档，`index.sqlite` 上游 issue/PR 索引。
- `records/records.sqlite`：全部任务记录索引。在 review-orchestrator 下：`python -m ro.archive --db ../../records/records.sqlite import ../../repos`。
- `notes/`：接口测试记录、路径迁移表、旧交接全文（只作历史查阅）。

## 规则

- 本文件、`opencode.jsonc`、repo-scout 的 `AGENTS.md`、流水线的 `capsule/` `prompts/` `config/reviewers.json` 受程序锁定：每次运行 orchestrate.py / gate.py 都会还原成用户批准的版本。模型不得修改它们，也不得运行 `python -m ro.policy lock`；规则要改时，向用户提出，由用户自己修改并执行 lock。

- 需要用户决策时，用 question 工具弹出 2–4 个选项（推荐项放第一个并标「（推荐）」，每项一句后果）；工具不可用时列编号选项并停下等待。能从代码、记录或常规做法确定的事不要问。
- GPT 一律 `#high`，以 `config/reviewers.json` 为准。
- 实施启动语见 `projects/review-orchestrator/prompts/operator.md`。实施者只读 `handoff/` 中的异议，不读审阅报告，不自行放行。
- 选题先 `python -m ro.upstream sync <owner/name> ../../repos/<仓库>/index.sqlite`（增量），不要再写抓取脚本或全量抓取。选题要写清用户可观察的后果和触发条件；证据不足只记线索，允许零项。
- 冻结前查看目标仓库 CI，把受影响的检查写进 `checks.json`。
- 默认只在本地记录。公开提交、发 Issue/PR 必须由用户指定具体条目并授权。
- 审阅结束后清空 `tmp/`；`work/` 里的 `.venv`/`.tox`/`target` 用完即删，需要时重建。
- 不在交接文件里保存凭证或聊天记录。Git 提交标题只写变更类型（fix、feat、test、docs）。
