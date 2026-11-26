# Review 跟进与贡献证明规则

这一层把上游状态、下一步工程动作和 Profile 草稿连接起来。它不发送评论、不合并 PR，也不会把提交次数当作被接受的贡献数。

## 状态与下一步

| 上游观察 | 本地状态 | 下一步 | 正式战绩 |
|---|---|---|---|
| 新 PR，没有可信维护者 Review | OPEN | 等待 Review | 否 |
| 有讨论，或审批针对旧的 head SHA | UNDER_REVIEW | 查看反馈、等待当前版本 Review | 否 |
| 有有效 Changes Requested | CHANGES_REQUESTED | 生成修订计划；经批准再修复和测试 | 否 |
| 当前 head 获得维护者批准 | APPROVED | 等待上游合并 | 否 |
| 上游 API 确认已合并且证据完整 | MERGED | 更新贡献台账、准备 Profile 草稿 | 是 |
| PR 关闭但没有合并 | CLOSED_UNMERGED | 记录结果和经验 | 否 |
| Issue 关闭，含 completed | CLOSED | 人工检查关闭原因和维护者证据 | 否 |
| 已获取维护者明确采纳评论，且用户确认其含义 | ACCEPTED | 更新台账、准备 Profile 草稿 | 是 |
| Issue 关闭原因为 not_planned 或 duplicate | REJECTED | 记录关闭原因 | 否 |

同一 Review 人的最后一个决定性 Review 生效；单纯 COMMENTED 不抹去已有批准或修改要求。
DISMISSED 不计为批准。批准只覆盖其 `commit_id` 对应的当前 head；
修改要求在换 head 后继续保留，直到新的决定性 Review 取代它。

`review_digest` 绑定当前 head 与生效 Review 内容，
可供修订批准使用。每次同步保留原始快照、评论链接、Review 状态和下一步动作。
同步不会覆盖正在进行的本地 IMPLEMENTING/TESTING；已观察到合并或关闭等最终结果除外。

CI 缺少可读接口、没有检查记录时显示 UNKNOWN；不会把“没有失败信息”写成“测试通过”。

## 正式贡献的证据门槛

正式台账仅接收 `provenance=live` 的记录，并重新核对保留的 GitHub API 快照：

- API 返回的仓库身份、提交编号、网页链接必须匹配。
- 仓库必须是公开、非 Fork 的真实上游，仓库所有者不能是配置中的贡献者账号。
- PR 或 Issue 作者必须是配置中的贡献者账号。
- PR 还要核对 `base.repo.id`、`base.repo.full_name` 和非 Fork 属性。
- PR 的 `merged=true`、带时区的 `merged_at` 和合并 SHA 必须同时存在。
  仅有 `merge_commit_sha` 不足以证明已合并。
- 为证明独立上游认可，本版本保守排除由提交者自己合并的 PR；这不是对其工程价值的判断。
- Issue 不使用关键词或关闭状态自动判断接受。用户必须给出已获取的维护者评论 URL、确认人及理由。
  决定绑定原评论内容摘要；评论修改或从完整快照消失后，该证明失效。
- 默认只把与仓库 owner 匹配的 GitHub `OWNER` 身份或配置中明确可信的维护者视为可采纳人。
  `MEMBER` 仅说明组织成员身份，`COLLABORATOR` 本身也不能证明足够权限。
  可信名单必须由操作者核对，是明确的信任决定。

一个 Finding 先提交 Issue、随后合并 PR 时，只形成一项贡献：
PR 成为主记录，Issue 成为支撑证据。重跑同步不增加重复条目。被撤销或被 PR 取代的旧条目保留历史，
但不再出现在正式成果中。

SQLite 本地证据链用于可追溯与一致性检查，不是第三方公证。具有本地文件修改权限的用户仍可修改数据库；独立验证以可打开的上游链接为准。

## 个人主页

只替换以下边界内的内容：

```markdown
<!-- repo-scout:contributions:start -->
...
<!-- repo-scout:contributions:end -->
```

没有边界时，在原文件末尾追加。边界丢失、重复或顺序错误时拒绝生成，防止吞掉个人介绍或其他项目内容。生成统一 Diff 和内容 SHA，供最终人工批准使用。

Profile 将提交链接与采纳证据并列展示，保留 AI 辅助的透明说明，不宣称所有工作均由用户独立完成。
历史贡献缺少本工具的验证或测试记录时，台账保持该字段为空，不补造运行结果。

## 当前边界

- GitHub public API 为本版本的证据来源；GitHub Enterprise 的不同域名需专门适配身份和 URL 校验。
- Issue 的“被认可”“已分诊”“明确接受”存在自然语言歧义，因此本版本把进入正式成果的语义判断留给用户。一般评论只生成跟进建议。
- 保守排除自己拥有的组织仓库不可完全自动完成：当前按上游 owner 是否等于个人账号、非 Fork 属性和非自己合并判断；
  若用户拥有其他组织，应在操作规则中排除它们。
- 一旦读请求失败，保留上次已知状态，并返回失败类型。不会把不可访问误当作关闭或删除。

## API 依据

实现时核对的官方文档（2026-11-24）：

- [Pull requests REST API](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)：
  PR 身份、base/head、merged 状态与 merge_commit_sha 的含义。
- [Pull request reviews REST API](https://docs.github.com/en/rest/pulls/reviews#list-reviews-for-a-pull-request)：
  Review 按时间顺序返回，state、commit_id、author_association 和原始链接。
- [Issues REST API](https://docs.github.com/en/rest/issues/issues#get-an-issue)：
  Issue 与 PR 的区别以及 state/state_reason。
- [Repository collaborator permissions](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user)：
  permission/role_name 的含义；COLLABORATOR 关联值不能代替实际权限检查。
