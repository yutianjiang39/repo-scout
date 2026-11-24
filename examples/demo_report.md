# DEMO — 从真实本地修复到模拟上游 Review 的闭环

DEMO — 离线合成示例，不是真实上游贡献。代码与测试在本机实际执行；CC 结论、人工审批、GitHub Review 与合并均为模拟。

## 实际运行结果

- 原始复现：**FAIL**，退出码 1，观察到 `AssertionError: 4 != 2`。
- 最小修复：**PASS**，退出码 0。
- Review 后补充测试：**PASS**，退出码 0，共三个测试。
- 原始仓库保持不变；修复和后续修改在独立 Worktree 中产生两个真实本地 Commit。

## 模拟 Review 闭环

| 事件 | 状态 | 正式贡献数 |
|---|---|---|
| simulated_review_requested | UNDER_REVIEW | 0 |
| simulated_changes_requested | CHANGES_REQUESTED | 0 |
| simulated_new_head_awaits_re_review | CHANGES_REQUESTED | 0 |
| simulated_maintainer_approved | APPROVED | 0 |
| simulated_upstream_merged | MERGED | 0 |

## 贡献真实性检查

虽然模拟 PR 到达 MERGED，所有数据库实体始终标记为 demo，正式贡献台账仍为 **0**。
Profile 草稿保留原有内容，并保持等待真实可验证贡献的占位说明。
本次没有真实 CC 审查、GitHub 搜索、公开 PR、维护者回复或上游合并。

## 可检查的文件

- `report.json`：阶段、SHA、测试运行 ID 和真实性检查。
- `data/contribution.db`：状态、批准和追加式历史。
- `reports/tests/`：实际 subprocess 测试日志和命令。
- `reports/validation/`：独立验证输入包与明确标记的模拟 CC 报告。
- `reports/submissions/`：最终草稿、Diff 与批准摘要。
- `reports/followup/`：模拟 Review 与回复草稿。
- `portfolio/`：正式贡献数为零的台账及 Profile 草稿。
