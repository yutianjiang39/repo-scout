# 架构与贡献闭环

## 1. 目标与 Stage A 实际检查

本系统管理从发现项目到上游认可的完整生命周期。成果指标是可打开上游链接核验的工程贡献，以及这些贡献覆盖的独立项目；待 Review、待修订、已获批准但未合并的工作单独展示。提交次数不能代替认可结果，也不承诺自动带来合作机会。

开始检查时，工作区只有用户上传的需求文档，没有现有 Repo Scout 源码、Git 仓库、数据库或 CLI。因此，本次在独立 `repo-scout/` 目录实现新的 Python MVP，没有重写旧代码，也不能声称已验证旧项目的 Phase 1、Phase 2 或兼容性。

| 检查对象 | 当时可确认的状态 | 本次处理 |
|---|---|---|
| 现有发现、审查模块 | 未提供实现 | 新建适配层与服务模块 |
| 旧数据模型、数据库 | 未提供 | 建立版本化 SQLite schema |
| 旧 CLI、配置、测试 | 未提供 | 新建独立入口、YAML 配置和离线测试 |
| GitHub / 模型账户连接 | 无可用的已验证连接 | 默认只读；密钥由运行者配置 |
| 历史上游成果 | 未提供真实记录 | 正式台账从空开始 |

以后接回旧项目时，应先检查真实 schema、命令与测试，再增加导入适配器。候选 Finding 可以导入，但导入文件不能直接取得确认、审批、已执行测试或正式贡献身份。

## 2. 模块分工

| 阶段 | 实现文件 | 产出与下一步 |
|---|---|---|
| 项目发现与筛选 | `github.py`、`discovery.py` | 去重候选、License/活跃度判断、维护者样本和带证据评分；用户选择重点项目 |
| 仓库与审查上下文 | `implementation.py`、`bundle.py` | 保留原始 clone；生成原路径、行号、哈希、依赖清单与 chunks |
| 多模型审查 | `providers.py`、`audit.py` | 可配置模型角色、反方意见、候选问题、覆盖情况与用量记录 |
| 独立验证 | `validation.py` | 当前上游 SHA、查重结果、CC 验证材料包、实际基线运行、结构化验证报告 |
| 修复与公开提交 | `workflow.py`、`implementation.py` | 批准范围、独立 Worktree、补丁、回归测试、Diff Review、PR/Issue 草稿与第二次批准 |
| Review 跟进 | `followup.py` | GitHub 原始快照、维护者反馈、CI、修订要求、下一步动作 |
| 贡献证明与主页 | `portfolio.py`、`workflow.py` | 去重贡献台账、上游证据、保留原文的 README 草稿及独立发布批准 |

这些模块由同一数据库连接，但可以独立执行。不需要为了跟进一个 PR，重新搜索和审查全部仓库。

## 3. 数据和状态设计

Python 3.11+ 负责 CLI 与工程操作；Pydantic 校验 `Finding`、`ValidationReport`、`Draft` 等关键输入；GitHub 快照和扩展记录在对应服务边界校验。SQLite 使用 WAL、事务、版本号和比较并交换，避免并发操作静默覆盖已批准内容。

| 数据表 | 职责 |
|---|---|
| `records` | 以 `kind + id` 存储版本化 JSON 记录；Repository、Finding、Submission 各自保留状态 |
| `events` | 保存前后状态、版本、原因、操作者、证据与载荷哈希；数据库触发器禁止原地修改或删除 |
| `approvals` | 审批范围、内容摘要、批准人、理由、到期时间和消费时间 |
| `operations` | 外部写入的持久化待发送记录，用于防止重复公开提交 |
| `cache` | GitHub 条件请求和模型内容缓存，按命名空间隔离 |

记录类型还包括验证、实际测试运行、Diff Review、草稿、反馈、原始快照、Issue 采纳决定、贡献条目、模型用量和批次。成果 JSON 是数据库的可读导出，不是允许任意伪造正式成果的入口。

Finding 的主要路径为候选、独立验证、确认、人工批准、修复、测试、最终审查、批准提交、等待 Review、接受结果。`REJECTED`、`ALREADY_FIXED`、`DUPLICATE`、`DESIGN_DECISION`、`BLOCKED` 等有独立处理路径。Issue 关闭和 PR 合并不会被强行压成同一个线性状态。

## 4. 三类批准与写入一致性

第一次批准绑定验证结论、上游版本、实施计划和文件范围；修订时还绑定当前 Review 与提交版本。第二次批准绑定完整公开草稿，包括目标、标题、正文、代码版本、Diff、测试证据与风险说明。Profile 批准绑定 README 内容、原文件版本和贡献台账版本。

批准默认 48 小时有效，允许的有效期为 1–168 小时；执行时按精确摘要查找，并单次消费。修改代码、正文、上游证据或目标后，需要重新形成具体可审查的结果。

外部写入先建立持久化操作记录，再进入 `SENDING`。成功响应后记录 `SUCCEEDED`；超时或结果不确定时保留 `UNKNOWN`。恢复操作通过只读查询、唯一标记或远端内容核对结果，不盲目重发。写 Token 的实际 GitHub 用户身份必须匹配配置账号。

## 5. 从 Review 回到修复

跟进模块保存原始评论 URL、Review 对应的 commit、CI 观察及反馈摘要。维护者要求修改后，系统生成明确的待修订动作；用户批准具体计划后，回到独立工作区完成修改、测试、Diff Review 和更新批准。

新 Review、远端 head 变化或上游默认分支变化都会影响既有依据。系统要求重新核对这些条件，不能把早期批准当作无限期的修复授权。回复建议保留为草稿；程序不自动联系维护者或合并 PR。

## 6. 正式贡献的准入规则

正式成果来自被核验的外部真实上游。PR 需要明确的合并状态、合并时间、合并 SHA 和独立认可证据。Issue 需要已获取的维护者评论、已核实身份，以及用户对“明确采纳”含义的确认。仅关闭、正面措辞、已批准但未合并、自己的 Fork、演示或导入数据都不能直接进入正式成果。

同一 Finding 的 Issue 和后续合并 PR 只算一项工程贡献，其他链接作为支持证据。证据发生变化时保留历史，同时撤回不再满足规则的正式条目。README 只修改指定贡献区块，保留个人介绍和其他项目，并明确 AI 辅助参与。

详细判定规则见 [Review 与成果政策](portfolio-policy.md)。本地 SQLite 证据链用于审计与一致性，不是第三方公证；最终独立核验仍依赖上游链接。

## 7. Provider 与 CC 的边界

统一 Provider 接口支持生成、Token 估算、健康检查和用量读取。OpenAI Responses、兼容 Chat Completions、Anthropic Messages、Gemini generateContent 分别适配；角色到 Provider/Model 的映射完全来自配置。任何审查模型只能创建候选意见。

CC 读取针对问题筛出的证据和上下文，独立判断候选是否成立；实际测试由经检查的执行器记录。可执行代码 PR 要求基线真实失败、修复后相同命令成功、源码和日志完整性一致；不能从模型说“PASS”推导测试已通过。外部 CC 报告导入保留操作者声明来源，不伪称由本程序运行。

## 8. 成本与运行边界

默认先筛少量项目，每个仓库最多五个高价值候选；按内容、上下文、Prompt 版本和模型配置缓存成功请求，重跑优先复用。模型调用有输入/输出预算、可配置并发和有界重试；价格未配置时不虚构成本。CC 使用独立调用预算，不把前序模型账单混为一项。

上下文映射是静态文件、符号、依赖和邻近上下文清单，不是完整跨语言调用图。秘密扫描采用启发式规则，不能替代人工检查。默认 Docker 执行禁网、去能力并限制资源；在主机执行需要明确选择。具体参数和已知限制见 [模型接口说明](api-models.md) 与 [运维指南](operations.md)。

## 9. 实施顺序与后续接入

本次先建立状态、证据、批准和台账，再连接发现、审查、CC、提交和 Review，最后用本地 Git fixture 与模拟 API 验证完整链路。下一步的生产接入依次为：配置只读 GitHub；审查一个真实项目；配置一个模型角色和低预算；在独立环境复现一个问题；验证 CC；最终由用户批准一项具体公开贡献。更多模型、项目和定时跟进应在这一条真实链路稳定之后逐步增加。

官方接口依据汇总在 [GitHub API 说明](api-github.md) 和 [模型 API 说明](api-models.md)。

关键原始参考包括 [GitHub REST 最佳实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)、[Pull Request Reviews](https://docs.github.com/en/rest/pulls/reviews)、[OpenAI Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create) 和 [Gemini generateContent](https://aistudio.google.com/api-reference/generate-content/generate-content)。
