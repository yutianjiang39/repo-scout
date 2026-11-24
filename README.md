# Repo Scout — 可验证的上游贡献闭环

一套 Python 3.11+ 命令行程序，把项目发现、候选问题、独立 CC 验证、最小修复、上游 Review、修订、采纳证据和 GitHub Profile 连成同一条可追溯记录。

**核心指标是被真实上游接受的工程贡献，以及这些贡献覆盖的不同上游项目。** 提交数、草稿数、Fork 内的合并和 GitHub Activity 不会成为正式战绩。

当前版本：`0.1.0`，可运行的本地 MVP。当前工作区只有需求文档，没有旧 Repo Scout 源码，因此本项目是独立新实现，没有重写或迁移一个未提供的旧项目。

## 1. 先跑完整演练

需要 Python 3.11+、Git。标准安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
repo-scout demo --output ./demo-run
```

演练会在新的独立目录中实际创建 Git 仓库与 Worktree，运行一个重复成交事件问题的失败测试、应用最小补丁、重新运行测试，再完成一轮模拟 Review 要求补测后的修订。

演练中的 CC 结论、批准、Maintainer Review 和合并响应都是明确标记的模拟数据；代码和测试确实在本地执行。全过程不请求网络、不调用付费模型、不公开提交。`demo-run/demo_report.md`、`report.json` 和测试日志给出完整结果。正式贡献保持 **0**。

如果当前 Python 已安装依赖，也可以不安装本包：

```bash
PYTHONPATH=src python -m repo_scout demo --output ./demo-run
PYTHONPATH=src python -m unittest discover -s tests -v
```

演练目录必须为空或不存在；程序不会覆盖旧演练证据。详细说明与一次实际运行的可移植结果在 [离线演练说明](docs/demo.md) 和 [示例报告](examples/demo_report.md)。

## 2. 已经实现的功能

| 阶段 | 可运行能力 | 质量边界 |
| --- | --- | --- |
| 发现与筛选 | 可配置 GitHub 查询、权重、去重、License/活跃度检查、维护者分析、证据评分 | 未知身份或数据不足保留 UNKNOWN，不把低 Stars 当作自动淘汰理由 |
| 审查副本 | Clone、保留原仓库、排除列表、原路径/行号、文件哈希、Python AST 分块、有限依赖上下文 | 敏感文件、symlink 和明显凭证不发送给模型；跨语言调用图不保证完整 |
| 多模型审查 | OpenAI Responses、兼容 Chat Completions、Anthropic、Gemini，模型与角色可替换 | 输出只能是候选问题；最多 5 个；预算未覆盖的部分明确为 PARTIAL |
| CC 验证 | 最新上游 SHA、Issue/PR 查重、独立任务包、CC CLI 结构化调用、外部 CC 报告导入、实际复现记录 | 模型自称 CONFIRMED/PASS 不足以通过；复现须引用真实 runner 记录和失败特征 |
| 修复与审批 | 第一道方案/文件范围批准、独立 Worktree、可选 CC 补丁、实际测试、最终 Diff Review | 代码修改有范围；较新的失败不能被旧 PASS 掩盖 |
| 提交管理 | PR/Issue/PR 更新草稿、Digest 绑定批准、Fork 身份与写 Token 账号核验、持久 Outbox | 没有最终批准不能 Push/创建；有歧义的写操作必须核对后恢复 |
| Review 跟进 | 评论、Review、CI、Changes Requested、当前 Head 的批准、修改方案、回复草稿 | 不自动回复、Merge；新 Review 会使旧修订方案失效 |
| 贡献台账 | SQLite + JSON、原始证据快照、不同上游项目统计、同问题 Issue→PR 去重 | 自有仓库、Fork、模拟记录、未合并 PR、仅关闭的 Issue 不计正式成果 |
| Profile | 读取真实 README，只替换贡献 marker 区域，生成 Diff、独立批准、SHA 并发保护 | 发布前重新同步接受证据；不覆盖原有介绍和其他项目 |

## 3. 初始化真实工作目录

```bash
repo-scout --home ./contributions init --username yutianjiang39
repo-scout --home ./contributions doctor
```

`--home` 和 `--config` 是全局参数，写在子命令前。下文为简洁省略 `--home`；省略时默认使用当前目录的 `.repo-scout/`。实际使用时固定同一个工作目录。

初始化写入 `config.yaml`，用户名和 Profile 目标可修改。默认目标为 `yutianjiang39/yutianjiang39`，**不会假定该仓库已经存在**；`profile-draft` 会先查询 GitHub 并验证身份。重复执行 `init` 时，省略 `--username` 会保留已有配置。

配置中只保存环境变量名称，不保存密钥：

```yaml
github:
  read_token_env: GITHUB_READ_TOKEN
  write_token_env: GITHUB_WRITE_TOKEN
tracking:
  interval_hours: 24
  trusted_maintainers: {}
providers: {}
roles: {}
```

在自己的终端通过凭证管理器或环境变量提供 Token。只读搜索可以匿名使用，但可用额度和端点权限可能受限；真正写入时需要单独的写 Token，程序会核对它的账号。不要把 Token 放进命令参数、JSON、Git URL 或配置文件。

模型 ID 不提供虚构默认值。参照 [模型配置与费用说明](docs/api-models.md)，填写自己账户可用的真实模型，并给角色指定 `provider`、`model`、`task`。未配置审查角色时不会请求付费 API。

## 4. 从发现进入独立验证

```bash
repo-scout run-discovery
repo-scout repositories
repo-scout approve-repositories OWNER/PROJECT --actor yutianjiang39 --reason "已检查维护者反馈、项目范围与贡献指南"
repo-scout clone OWNER/PROJECT
repo-scout prepare OWNER/PROJECT
repo-scout audit OWNER/PROJECT
repo-scout findings
```

`OWNER/PROJECT` 和后文的 `FINDING_ID` 都是占位符，替换为实际记录。发现报告中包含 `raw_candidates.json`、`shortlist.csv`、`excluded_repos.csv`、维护者分析和评分证据。`run-discovery` 不自动批准仓库。

审查返回的是候选问题。`verify-ai` 使用同一缓存与配置执行审查/反方角色，也不会将候选升级为已验证。

对一个值得继续的候选执行：

```bash
repo-scout preflight FINDING_ID
repo-scout cc-packet FINDING_ID
```

Preflight 获取最新上游身份、默认分支和 SHA，重新查重，创建只用于验证的独立 Worktree；旧 SHA 的验证目录保留。CC 任务包包含当前源码、必要测试/调用上下文、贡献指南、历史与原始候选意见。仓库内容始终按不可信数据处理。

先让 CC 设计最小复现，在输出的 `validation_workspace` 新增回归测试；未修改的上游代码须保持原样。检查命令和依赖后，记录真实执行：

```bash
repo-scout test FINDING_ID --purpose baseline --reviewed --argv '["python", "-m", "unittest", "discover", "-s", "tests"]'
```

默认使用已有的 Docker 镜像，网络关闭、无宿主凭证、限制权限与资源，不自动下载镜像、不安装上游依赖。若决定在自己的已审核环境执行，需要明确选择：

```bash
repo-scout test FINDING_ID --purpose baseline --reviewed --runner host --allow-host-execution --argv '["python", "-m", "unittest", "discover", "-s", "tests"]'
```

Host runner 不是沙箱；只用于已经检查过的命令和仓库。程序去掉密钥环境变量，记录真实退出码、源码哈希、日志哈希、时间与命令。失败测试返回退出码 1，不能因为它是“预期失败”就记成 PASS。

独立 CC 验证有两个入口：

```bash
repo-scout validate FINDING_ID --cc --budget 2
# 或：导入实际进行过的外部 Claude Code 独立验证
repo-scout validate FINDING_ID --report ./validation.json --attest-cc
```

自动入口使用 `claude --bare -p`，禁用工具和 MCP，只传已筛选的当前代码及观察证据；不会加载上游 hooks/CLAUDE.md 来执行操作。需要本机 Claude Code 和 `ANTHROPIC_API_KEY`；缺失时提供清楚错误并保留任务包。`--budget` 是 CC 的客户端估算上限，不是账单保证。

导入报告遵循任务包中的 `validation.schema.json`。程序验证 Finding ID、SHA、查重完整性、真实 baseline run ID 和实际日志中的失败特征。导入注明 operator attestation，不伪装成内置 runner 已调用过 CC。无法复现的普通代码问题不得直接进入修复 PR；保留 PROBABLE/BLOCKED，或依据明确证据先讨论 Issue。安全漏洞转向 `SECURITY.md` 私下披露流程。

## 5. 第一道人为批准、修复、实际测试

```bash
repo-scout approve FINDING_ID --actor yutianjiang39 --plan "修复重复事件处理，并增加回归测试" --scope src/orders.py --scope tests/
repo-scout implement FINDING_ID --cc --budget 2
```

也可以不传 `--cc`，只创建独立分支和 Worktree，然后在该目录用交互式 CC 完成修改，或通过 `apply-patch FINDING_ID fix.patch` 应用补丁。自动 CC 仅返回批准范围内的补丁，没有测试成功权限。

```bash
repo-scout test FINDING_ID --purpose regression --reviewed --argv '["python", "-m", "unittest", "discover", "-s", "tests"]'
repo-scout commit FINDING_ID --message "fix: handle duplicate order events idempotently" --name "Your Name" --email "your-git-email"
repo-scout review-diff FINDING_ID --actor yutianjiang39 --assessment "核对根因、边界测试、兼容性与变更范围" --approve
```

应在修复后成功运行同一条 baseline 命令。项目要求的 lint、集成测试和 benchmark 分别通过 `test --purpose ...` 记录。`review-ai` 可以调用配置的 `final_diff` 角色提供建议，最终 `review-diff` 仍须有明确审查者与结论。

## 6. 第二道批准：具体公开内容

先将完整说明写入 `pr_body.md`。程序会在草稿中注明 AI-assisted，并展示目标、文件、测试、风险和 Digest。

```bash
repo-scout prepare-pr FINDING_ID --fork yutianjiang39/PROJECT --title "fix: handle duplicate order events" --body-file ./pr_body.md --risk "保留现有接口；新增事件去重回归覆盖"
repo-scout approve-submit FINDING_ID --digest DIGEST_FROM_DRAFT --actor yutianjiang39 --reason "已审阅完整正文、Diff、测试与目标上游"
repo-scout submit FINDING_ID
```

程序要求 Fork 已经存在且确属该上游；不会擅自创建新的公开 Fork。`prepare-issue` 使用同样的内容批准流程，不需要 Fork。

提交前再次检查上游 SHA、查重结果、最新 Review（修订时）、写 Token 账号、Fork 父仓库、完整提交历史和测试。只 Push 批准的不可变 SHA，不使用可能在检查后改变的 `HEAD`。不会 force-push、直接写上游默认分支或自动 Merge。

草稿、测试、代码或 Review 变化后，重新测试/准备草稿/批准。改变已批准草稿的正文可再次运行 `prepare-pr` / `prepare-issue`；新的 Digest 不继承旧批准。

若请求响应丢失，状态为 UNKNOWN，不自动重发：

```bash
repo-scout reconcile-submit FINDING_ID
```

该命令通过真实上游链接/标记或 PR Head 核对结果。服务端明确拒绝的写操作为 FAILED，修正问题后需要新的明确批准。UNKNOWN 没有“强行重试”开关，以免重复公开 Issue/PR。详见 [运维与恢复](docs/operations.md)。

## 7. 获得 Review 后继续到采纳

```bash
repo-scout watch --once
repo-scout feedback
repo-scout followup-report
repo-scout status
```

`watch` 是单次同步，遵循 `tracking.interval_hours`，适合 cron/系统任务计划程序。`--force` 可明确立即查询。程序不会启动未被管理的后台进程，也不会自动发送 Maintainer 回复。

Changes Requested 需要新的方案批准。若原 Preflight 已过期，或上游已变化：

```bash
repo-scout refresh-revision FINDING_ID
# 根据最新源码重新复现和 validate；然后：
repo-scout approve FINDING_ID --actor yutianjiang39 --plan "依据具体 Review 增补边界覆盖" --scope src/orders.py --scope tests/
repo-scout implement FINDING_ID
# 完成修改、实际测试、本地 commit 和 review-diff 后：
repo-scout prepare-update FINDING_ID --fork yutianjiang39/PROJECT --title "fix: handle duplicate order events" --body-file ./revision_notes.md --risk "仅处理本轮 Review 范围"
repo-scout approve-submit FINDING_ID --digest NEW_DIGEST --actor yutianjiang39 --reason "已审阅本轮修订"
repo-scout submit FINDING_ID
```

更新通过已批准的正常分支 Push 进入原 PR，保留上游讨论。新的 Review 或远端 Head 会使旧修订批准失效；如 rebase 出现冲突，冲突留在该独立 Worktree，需要先解决并重新测试。

自己已经提交过的外部 PR/Issue 可只读纳入跟进：

```bash
repo-scout track OWNER/PROJECT 123 --type PR
repo-scout track OWNER/PROJECT 456 --type ISSUE
```

程序核验真实作者与目标上游；同一记录重复 track 不重复计数。若 Issue 后续对应同一问题的 PR，通过 `--finding-id` 关联，台账只算一次贡献。

Issue 只有得到明确的维护者采纳证据才能进入正式台账：

```bash
repo-scout accept-issue SUBMISSION_ID --evidence-url https://github.com/OWNER/PROJECT/issues/456#issuecomment-ID --actor yutianjiang39 --rationale "维护者明确确认问题并采纳修复方向"
```

必须引用刚从 GitHub 获取的具体维护者评论；正面措辞、关闭状态和自己写的摘要不能单独构成证据。评论被编辑/删除，或 Issue 被明确拒绝时，旧证据会失效。Maintainer 身份不从 MEMBER/COLLABORATOR 自动推断；经过核实的协作者可列入 `tracking.trusted_maintainers`。

## 8. 贡献台账与 Profile

```bash
repo-scout portfolio-sync
repo-scout portfolio
repo-scout profile-draft
```

正式台账只展示满足证据规则的记录。PR Submitted、Under Review、Changes Requested、Approved 均独立记录；其中 Approved 不是 Merged。自己 Merge 的 PR 不作为独立上游接受证据，保留在历史中。

Profile 仅编辑下列区域，其余内容逐字保留：

```markdown
<!-- repo-scout:contributions:start -->
... verified contributions ...
<!-- repo-scout:contributions:end -->
```

`profile-draft` 获取实际 README，并重新同步贡献证据；生成完整 Markdown 和 Diff。第一次使用时追加该区域；marker 重复/损坏则拒绝。审批独立于单个 PR 的批准：

```bash
repo-scout approve-profile --digest PROFILE_DIGEST --actor yutianjiang39 --reason "核实项目、接受证据和个人介绍保留情况"
repo-scout profile-publish
```

发布前再次核验上游接受证据和远端 README SHA，防止旧证据展示或覆盖同时发生的个人主页编辑。

## 9. 数据、恢复与成本

工作目录保存 `data/contribution.db`、`repositories/`、每个 Finding 的 `workspace/`、阶段 `reports/` 和 `portfolio/`。同一 SQLite 文件存放类型记录、不可修改的事件历史、审批、持久 Outbox、HTTP/模型缓存和模型用量，减少多个数据库之间的同步问题。导出的贡献台账是独立 JSON。

每个模块可独立执行；模型调用缓存覆盖内容、Prompt 版本、Provider、模型及参数。修改代码后只重新处理变化任务。没有足够证据时可停在 PROBABLE/BLOCKED，不为了推进状态而制造贡献。

该程序在个人受信任机器运行。SQLite 事件禁止普通 UPDATE/DELETE，但不声称可以防止机器管理员改写整个数据库。秘密扫描是启发式；测试隔离和代码审核仍有必要。Docker、Claude Code、GitHub 凭证和真实模型计费需在使用环境配置。

## 10. 验证状态与本次环境边界

测试以 `unittest`、MockTransport/MockGitHub 和本地临时 Git 仓库运行，不要求真实 API Key。覆盖了发现、去重、License、维护者 UNKNOWN、审查副本、秘密排除、分块、四类 Provider、缓存/预算、状态机、实际复现、审批、Worktree、提交、Review 修订、台账、Profile Diff 和 CLI 退出码。

本次环境没有 Claude Code、Docker 或 GitHub 读写凭证；匿名 GitHub REST 直连烟测未成功。**没有进行真实模型调用、真实 Docker 执行、真实上游公开提交或 Profile 发布，也没有声称获得实际贡献成果。** HTTP 协议已按官方文档实现，真实服务联调仍需在具备网络和凭证的机器上完成。

更多内容：

- [架构与 Stage A 检查](docs/architecture.md)
- [运维、命令、恢复与备份](docs/operations.md)
- [需求验收映射与边界](docs/acceptance.md)
- [模型接口与成本](docs/api-models.md)
- [GitHub 接口依据](docs/api-github.md)
- [真实贡献判定规则](docs/portfolio-policy.md)
- [Claude Code 接入依据](docs/claude-code.md)

