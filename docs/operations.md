# 运行与恢复指南

## 1. 先确认本次交付验证到了哪里

源码与自动化测试已实现；测试使用本地 Git fixture 和模拟 API，
无需真实密钥。本次执行环境的匿名 GitHub REST 烟测遭遇传输失败，
未完成真实 GitHub 网络联调；Claude Code CLI 不可用，
因此没有运行真实 CC 会话，也没有付费模型调用。没有创建公开 Issue/PR、推送 Fork、发布 Profile，
或获得真实上游接受记录。

演示展示程序如何处理证据与状态，不代表用户已经在示例项目作出贡献。正式台账初始为空是正确结果。

## 2. 本地安装与离线检查

使用 Python 3.11+ 和 Git；项目依赖 Pydantic、httpx、PyYAML。进入源码目录后按 README 安装，首先执行：

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
repo-scout --help
```

未安装可编辑包时，离线测试使用：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

以已安装版本的 `repo-scout <command> --help` 为参数依据。
阶段命令可以单独执行；先查看状态与待办，再执行相应下一步，不需要重复运行整个链路。

首次初始化与离线演示可直接使用：

```bash
repo-scout --home ./scout-data init --username yutianjiang39
repo-scout --home ./scout-data doctor
repo-scout demo --output ./demo-work
```

`demo-work` 必须是新的空目录。真实运行继续使用同一个 `--home`；全局 `--home` 和 `--config` 参数写在子命令之前。

## 3. 准备配置与凭证

账号默认 `yutianjiang39`，Profile 默认 `yutianjiang39/yutianjiang39`。
两个值都在配置中；生成 Profile 草稿前会实际读取仓库和 README，
404 不会被当作已存在。

| 设置 | 使用方式 |
|---|---|
| `username`、`profile_repository` | 改为实际贡献账号及对应个人主页仓库 |
| `github.read_token_env` | 默认 `GITHUB_READ_TOKEN`；先配置只读能力 |
| `github.write_token_env` | 默认 `GITHUB_WRITE_TOKEN`；只在准备进行已批准的公开写入时提供 |
| `search.categories` | 各领域的 name、query、weight；默认资源权重为 40/28/20/12 |
| `maintainer` | 按需限制采样仓库、PR、Issue 和时间窗口；不是无限采集 |
| `tracking.trusted_maintainers` | `owner/repo: [login, ...]`，只放入已经核实身份的维护者 |
| `providers`、`roles` | 默认空；填写真实可用模型 ID 后再启用审查 |
| `audit` | 输入/输出 Token、美元预算、并发、Prompt 版本与 chunk 限额 |
| `execution` | 默认 Docker；镜像、超时和执行方式可配置 |

`maintainer.trusted_maintainers` 若未单独设置，
继承 `tracking.trusted_maintainers`。自定义维护者名单属于操作者的明确身份判断，
不能从用户名、组织成员身份或 AI 推测生成。

密钥通过本机凭证管理或环境变量提供，不写入 YAML、命令历史示例、报告或仓库。
首次运行不需要配置所有模型；一个主审角色已经可以产生待独立验证的候选。完整模型字段和成本说明见 [模型配置](api-models.md)。

## 4. 首次真实运行顺序

| 步骤 | 操作目的 | 应检查的产物 |
|---|---|---|
| 发现与筛选 | 找到少量值得参与的真实上游 | 候选 JSON、shortlist、排除原因、维护者分析和评分范围 |
| 批准目标仓库 | 确认方向、许可证、维护方式和可承担的贡献范围 | 批准仓库清单 |
| Clone 与 prepare | 保留原仓库，准备只读审查副本 | manifest 中的保留/排除原因、原路径、SHA、chunks |
| Audit 与反方审查 | 找到有具体源码证据的问题 | findings、verification、PARTIAL/COMPLETED 范围与用量 |
| Preflight 与 CC | 重新核对当前上游、重复项、保护逻辑及设计意图 | CC 材料包、结构化报告和实际失败日志 |
| 第一轮批准与修复 | 批准具体计划和文件范围，在独立 Worktree 修复 | 实施任务、补丁、原始基线和修复后测试记录 |
| Diff Review 与草稿 | 确认小范围修复、兼容性和准确公开描述 | 完整正文、changed files、测试、风险、Diff、审批摘要 |
| 最终批准与提交 | 将批准过的具体内容推送至正确 Fork/上游 | 提交响应、URL、Outbox 结果和待 Review 状态 |
| 跟进 | 获取新的 Review/CI，识别下一步 | 原始反馈链接、修订计划、更新批准或接受证据 |
| 台账与 Profile | 将真正被认可的贡献转成可核验展示 | 去重台账、贡献摘要、保留原文的 README Diff |

人工批准是程序中的审计记录，不是一次同意后无限发布。程序内的第一轮批准不能替代最终公开草稿批准；Profile 更新也有独立的最终批准。

### 已核对的 CLI 入口

| 目的 | 命令 |
|---|---|
| 独立执行发现各步骤 | `discover`、`screen`、`analyze-maintainers`、`score` |
| 运行完整发现阶段 | `run-discovery`；结果仍需 `approve-repositories` |
| 准备与审查批准仓库 | `clone`、`prepare`、`audit`；也可 `run-audit` |
| 导入候选或查看状态 | `import-findings`、`findings`、`finding`、`status`、`history` |
| 独立验证 | `preflight`、`cc-packet`、`validate` |
| 实施与证据 | `approve`、`implement`、`apply-patch`、`test`、`commit` |
| 最终评审与 PR/Issue 草稿 | `review-ai`、`review-diff`、`prepare-pr`、`prepare-issue` |
| 最终批准与写入 | `approve-submit`、`submit`；未知结果用 `reconcile-submit` |
| Review 修订 | `refresh-revision`、重新 `validate`/`approve`/`implement`，完成后 `prepare-update` |
| 跟进已有贡献 | `track`、`watch`、`check-pr`、`check-issues`、`feedback`、`followup-report` |
| 正式成果与主页 | `accept-issue`、`portfolio`、`portfolio-sync`、`portfolio-preview`、`profile-draft`、`approve-profile`、`profile-publish` |

`verify-ai` 是按当前角色配置重跑审查、复用缓存的入口，不独立授予任何确认状态。
`run-contribution` 只为已经获批的 PR 工作准备工作区和待办，
不越过测试及最终公开批准。

## 5. 运行复现与回归测试

先检查命令、依赖与它们会执行的脚本，再使用执行器。默认 Docker 不联网、不自动拉取镜像，
镜像必须预先准备；上游依赖若尚未装入镜像，测试不能凭空成功，应据实记录环境阻塞。

同一 Finding 的验证和实施工作区分开。基线运行使用当前上游代码；新增复现测试可以作为单独新文件加入，
但不能先修改待验证代码再宣称复现。修复后需要执行相同命令，日志、退出码、源码树和 commit 都会留下记录。
单纯打印“PASS”或导入一段 JSON 不能代替执行。

例如，检查过脚本且测试依赖已在镜像中就绪后，可运行下列形式；`FINDING-ID` 和测试路径须替换为实际值：

```bash
repo-scout --home ./scout-data test FINDING-ID --purpose baseline --reviewed --argv '["python", "-m", "pytest", "tests/test_regression.py"]'
repo-scout --home ./scout-data test FINDING-ID --purpose regression --reviewed --argv '["python", "-m", "pytest", "tests/test_regression.py"]'
```

第二条需在批准后的修复工作区已经完成修改时运行。`--argv` 是 JSON 参数数组，
不执行 shell 展开；需要 shell 功能时也必须把解释器作为明确、已审查的命令处理。

若明确选择在主机执行，子进程仍只得到精简环境，并对输出和记录参数做凭证脱敏；这不等于 OS 沙箱。CI、安装脚本或本机依赖仍需要人工检查。

CC 自动模式需要安装兼容的 `claude` CLI 并提供 `ANTHROPIC_API_KEY`。
程序使用单独临时目录、禁用工具和 MCP 的结构化调用，让 CC 针对材料出具报告或补丁。
也可在外部独立 CC 会话完成，再明确声明来源导入报告；报告仍须通过 SHA、状态、真实复现与日志完整性检查。

## 6. 常见恢复路径

| 情况 | 程序行为与处理方式 |
|---|---|
| GitHub 429 / 限流 | 尊重 Retry-After；长等待返回明确限流信息，稍后再运行只读同步 |
| 模型预算不足 | 保留成功结果和缓存，批次标记 PARTIAL；缩小范围或调整预算后续跑 |
| 模型超时 | 费用结果可能未知；记录未知状态，不把它当成零成本成功 |
| 上游 SHA 已变化 | 原批准或验证依据不能直接沿用；重新 preflight，确认问题仍存在，并保留旧证据 |
| 新重复 Issue/PR | 重新检查新候选和设计讨论，必要时标为 DUPLICATE 或 ALREADY_FIXED |
| 维护者新 Review 或 head 变化 | 刷新反馈，重新形成具体修订计划和批准 |
| 写入响应丢失、Outbox 为 UNKNOWN | 使用只读 reconcile 查找唯一标记或远端内容；不能为了“重试”删除审批或操作记录 |
| 没有唯一远端结果 | 保持 UNKNOWN，人工检查上游；该版本不会根据暂时搜索不到就擅自重发 |
| Profile README 被别人修改 | 重新读取原文、生成 Diff 并重新批准，避免覆盖其他内容 |
| 采纳评论被修改或删除 | 同步后撤回失效证明，保留历史；不要手工把状态改回 ACCEPTED |

## 7. 长期维护节奏

默认跟进间隔为一天，可配置为更长周期；手动同步可用于刚收到 Review 的工作。
根据待办推进：补充测试、解释设计、完成批准范围内的修订、等待 Review 或合并。
没有新证据时不制造回复，也不为增加数字把一个修复拆成多个贡献。

`watch` 是一次性增量同步，不是后台常驻进程。可以把以下命令交给 cron、systemd timer 或任务计划程序；
本次交付没有在用户机器创建任何定时任务：

```bash
repo-scout --home ./scout-data watch --once
```

刚收到 Review 而需立即检查时使用 `watch --force`。`portfolio-sync` 会强制刷新已跟踪上游并生成本地成果预览；
真正发布前仍需要读取真实 Profile 的 `profile-draft`。

长期观察正式贡献数、独立上游项目数、Changes Requested 和待处理项。Repo 活跃度只是寻找合作对象的输入，不能作为个人成果证明。

## 8. 备份与升级

至少备份工作根目录中的数据库、报告、贡献台账和未提交的 Worktree。数据库开启 WAL，
运行中直接只复制主 `.db` 文件可能遗漏 WAL 中的数据；停止程序后复制整个相关目录，
或使用 SQLite backup API。审批、原始快照和 Outbox 不能在普通清理中丢弃。

当前 schema 版本为 1；未知版本拒绝打开。尚未提供旧 Repo Scout 数据库自动迁移器，
也没有将多机器共享 SQLite 作为部署方式。已有项目接入时应先做映射和迁移测试。

更完整的覆盖与限制见 [验收映射](acceptance.md)。
