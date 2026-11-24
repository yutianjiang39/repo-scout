# 最近交接 · 2026-11-24

- **已实现**：`src/repo_scout/pilot.py` 对 5 个公开库按 HEAD SHA 巡检文档链接；搜索上游重复项，本地保存草稿；`review` 给中文摘要，`show` 给证据和 diff，`judge` 按 SHA 记录验收，`stats` 给指标。模型调用为零。限流恢复时间记录逻辑已加入。
- **已验证**：`tests/test_pilot.py` 两项测试通过。本机首次真实运行完成 `hyperliquid-dex/hyperliquid-python-sdk` 的 3 份文档，0 条候选；在 `encode/httpx` 遇到 403 限流后停止。此次数据库在 `pilot-readonly-week/pilot.sqlite3`，不要与旧的 `pilot-data/` 混用。
- **自动试跑**：未部署服务器，未完成连续一周运行。自动断链队列没有人工验收样本；下方手动发现的 Issue 不计入其验收比例。服务器部署暂缓，恢复试跑时按 `docs/pilot.md` 操作，并用 `stats`、`review` 核对实时数据。

## 本地人工巡检：HL-POETRY-01 已发布

- **发现**：`pilot-data/manual-01/` 的五库 tick 检查 15 份文档，0 候选；另扫同一批 SHA 的 191 份 Markdown，0 条断链。人工检查贡献指南时排除了已有 #277 提案的 Homebrew 路径问题，发现 Hyperliquid SDK 的 README 示例使用 Poetry 1.4.1，而 Makefile 的完整锁文件更新命令使用该版本不支持的 `--regenerate`。
- **交叉复核**：固定 SHA 为 `2fdb18f9517675ea03695a0962bd19eece9c83f0`。GPT 实际运行 Makefile 中的命令，得到 `The option "--regenerate" does not exist`。CC 核对 README、Makefile、锁文件和 CLI 选项，没有重跑失败命令。这是两条互补证据，不是两次独立环境复现。
- **发布**：用户审查询问式英文草稿并明确指定发送，发送前核对默认分支 SHA 并检索相关 Issue/PR。已发布 [Hyperliquid Python SDK #348](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/issues/348)，没有 PR，勿重复发送。

## 下次巡检约定：CC 先看证据，再看 GPT 结论

沿用已跑通的本机 GPT 环境与 Claude Code 调用方式。以下是人工协作约定，尚未实现为自动流水线；服务器 API 配置未验证。

1. **GPT 筛选和发现**：记录仓库、固定 SHA、相关文件和原始证据，检查上游是否已有 Issue 或 PR。
2. **CC 独立核查**：第一轮只给仓库、SHA、文件、原始证据和中性问题（例如“该版本的 CLI 是否接受这个参数？”），不给 GPT 的判断、草稿或建议修法。先让 CC 写下自己的结论，并分别标明亲自复现、源码或文档证实、未核实项。随后才对照 GPT 结论；有分歧就补证据，不按票数决定。
3. **GPT 起草，CC 润色**：维护者意图不明时起草询问式英文 Issue；修法明确且可验证时准备英文 PR 说明和 diff。润色若引入新断言或改变补丁，返回核查。
4. **用户审查并指定发送**：本地展示最终正文、证据及适用的 diff。验收通过只记录判断；用户明确指定发送后才发布。正文或补丁有变更需重新确认，后台巡检保持只读。

## Repo Scout 本地可靠性改进（未部署）

两阶段手工 CC 操作见 `docs/claude-code.md`。增加私有配置过滤、筛选偏好、阈值和候选上限、模糊模型请求防重放与内容绑定校验。按用户要求取消 6.1，美元预算默认仍为 null。使用临时数据库完成 119 项测试（118 通过，1 项因 Windows 无符号链接权限跳过），构建 sdist 和 wheel。没有迁移运行数据库、提交代码或发布。CC 整体审查两次超时，不能记录为双验收通过。
