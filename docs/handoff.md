# 最近交接 · 2026-11-24

- **已实现**：`src/repo_scout/pilot.py` 对 5 个公开库按 HEAD SHA 巡检文档链接；搜索上游重复项，本地保存草稿；`review` 给中文摘要，`show` 给证据和 diff，`judge` 按 SHA 记录验收，`stats` 给指标。模型调用为零。限流恢复时间记录逻辑已加入。
- **已验证**：`tests/test_pilot.py` 两项测试通过。本机首次真实运行完成 `hyperliquid-dex/hyperliquid-python-sdk` 的 3 份文档，0 条候选；在 `encode/httpx` 遇到 403 限流后停止。此次数据库在 `pilot-readonly-week/pilot.sqlite3`，不要与旧的 `pilot-data/` 混用。
- **尚未验证**：未部署服务器，未完成连续一周运行，尚无人工验收样本。文档试跑不能代表核心代码审查质量或模型费用。没有向上游发布。
- **下一步**：服务器就绪后按 `docs/pilot.md` 配置只读访问和 systemd 定时器；一周后汇总恢复、费用、验收比例，再决定是否扩容。接手时用 `stats`、`review` 核对当前数据，勿把上述快照当作实时状态。
