# 离线贡献闭环演练

```bash
repo-scout demo --output ./demo-run
```

输出目录必须是新目录或空目录，重复运行请选择另一个目录。无需 API Key、GitHub Token、Claude Code 或网络。
演练只运行程序内置、可直接阅读的 Python 标准库测试。

## 实际执行的工程工作

演练创建一个合成的单订单成交量聚合器。初始实现直接求和，收到相同 trade ID 的重传消息时重复计数。
程序建立真实 Git 仓库，在独立验证 Worktree 中运行回归测试，观察到 `AssertionError: 4 != 2`，
并保存命令、退出码、源码哈希和原始日志。

第一轮修复在另一独立 Worktree 中加入 trade ID 去重，并实际运行同一条回归测试获得 PASS。
随后生成本地 Commit、Diff 审查记录、PR 草稿和绑定到精确草稿内容的批准记录。

模拟维护者要求补充“不同 trade ID 的相同数量”和“非相邻重复消息”的测试后，
程序再次经过修改方案批准、补充测试、真实运行、Commit、Diff 审查和 PR 更新草稿批准。
第二轮包含三个真实执行的测试。

原始仓库内容与 HEAD 始终保留不变。

## 明确模拟的部分

CC 独立审查结论、人工审批、仓库元数据、重复搜索、PR、维护者身份、Review 和上游合并都由演练 fixture 模拟。
没有调用真实模型、搜索 GitHub、推送分支、创建 PR 或联系维护者。

Review 的模拟状态依次为：

1. UNDER_REVIEW。
2. CHANGES_REQUESTED。
3. 新的本地 Commit 已完成，但仍等待复审。
4. APPROVED。
5. MERGED。

所有数据库实体始终是 `provenance: demo`；即使模拟状态是 MERGED，
正式 Contribution Ledger 仍为 **0**，Profile 不会展示演练数据。
此处的零证明了真实性门槛正在工作。

## 阅读结果

- `demo_report.md`：可直接阅读的演练结果。
- `report.json`：阶段、实际测试运行 ID、两轮真实本地 SHA、Review 状态、真实性检查。
- `reports/tests/`：真实执行日志与命令。
- `reports/validation/`：CC 上下文包和有明确 DEMO 标记的模拟审查结论。
- `reports/submissions/`：最终更新草稿、Diff 与批准摘要。
- `reports/followup/`：模拟 Review、待办和回复草稿。
- `data/contribution.db`：状态记录、批准和追加式历史。
- `portfolio/`：正式贡献数为零的台账、Profile 草稿与 Diff。

仓库的 `examples/demo_report.md` 与 `examples/demo_result.json` 来自开发期间实际执行的一次离线演练。
可移植 JSON 示例省略了机器上的绝对路径；自己运行演练后会得到完整路径与日志。
