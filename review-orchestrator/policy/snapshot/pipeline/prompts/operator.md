# 实施会话启动语

规则都由程序执行，不需要写在提示词里。把下面这段连同 PR 编号发给实施会话：

---

你是实施者。按 `portable-review-pipeline/` 处理 **<仓库> #<编号>**，关卡 `gpt,claude`。

1. 读 `prompts/implement.md`。分析问题，写详细计划，实现修复和回归测试，在本地提交成「基线 → 候选」两个 commit。
2. 写 `requirement.md`、`commands.md` 和 `checks.json`（参照 `templates/checks.example.json`）。
3. 运行 `orchestrate.py start ...`。之后不要自己启动审阅，不要读报告。
4. 退出码 3（REVISE）时只读 `handoff/` 中的异议，修改后再运行 `start`。如果你有反证、不打算改补丁，用 `orchestrate.py recheck <角色> <反证.md>`。
5. 退出码 0 后写简明 PR 正文，运行 `orchestrate.py final <正文>`。到 `ready` 后停下，等用户授权。
