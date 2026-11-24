# Strict Dual Review & Human Approval

一个审查者可以发现候选，不能确认它。Finding、最终 Diff、实际提交包、Profile 分别开轮次。每轮两份独立报告先落库，再分别向两个全新会话提供两份初审和当前证据。最终两份有依据的 PASS 且无未解分歧，才取得该轮技术验收凭据。不得投票、以汇总意见替代报告、或以技术验收代替人工授权。

## 操作者与受信采集器

在操作者账户设置 `REPO_SCOUT_REVIEW_TRUST` 指向受保护的 UTF-8 JSON 文件；不从被审仓库、报告或 SQLite 导入信任根。示意（省略实际公钥）：

```json
{"reviewer_a":{"provider":"provider-a","model":"resolved-model-a","model_version":"version-a","canonical_model_id":"upstream/model-a","public_key":"BASE64_RAW_ED25519_PUBLIC_KEY_A"},"reviewer_b":{"provider":"provider-b","model":"resolved-model-b","model_version":"version-b","canonical_model_id":"upstream/model-b","public_key":"BASE64_RAW_ED25519_PUBLIC_KEY_B"},"human":{"public_key":"BASE64_RAW_ED25519_OPERATOR_PUBLIC_KEY"}}
```

签名端是部署信任边界：每个采集器核查供应商实际返回的模型身份、版本和会话；不可解析的代理别名不签。私钥不进入 Repo Scout、模型提示或模型可调用的工具。仅在无工作区访问、无对方会话记忆的新会话中提供指定 packet。禁止签署从任意 JSON 复制来的自报身份。框架验证受信回执，不声称能观察服务商内部模型路由。操作者签名使用单独的私钥，在只能由人操作的签名端预览内容后签署。模型进程不得访问操作者配置、SQLite 写权限、私钥或 GitHub 写 token。拥有管理员权限的人仍然可以替换程序、库或信任根。

## 操作顺序

独立 HTTP 采集器参考实现：在隔离账户用 `python scripts/collect_review.py collector.json packet.json`，输出导入用回执。配置字段见脚本开头。它验证 context_digest，只签本次真实无工具请求的结构化响应；网关未返回固定模型版本或请求 ID 时不签。没有为 CC 订阅终端的裸 JSON 提供签名捷径。需要从 `review-packet` 保存到文件时使用 UTF-8。

1. `preflight ID`，运行适用的 baseline。沿用 CC blind/comparison 导入，或 `record-assessment ID report.json` 添加其他角色的结构化证据。它们都不能单独确认 Finding。
2. `review-start ID finding`；为两个角色分别执行 `review-packet ID finding reviewer_a independent` 等。采集器对 `{binding,decision}` 的排序、紧凑、非 ASCII 转义 UTF-8 JSON（`common.canonical`）签名，返回 `{binding,decision,signature}`；签名用标准 Base64 编码。每个报告执行 `review-import ID finding report.json`。`decision` 包含 session_id、verdict、evidence（kind/reference）、disagreements、rationale、reviewed_at。
3. 两份初审入库后，用同样命令的 `final` 阶段交换意见，再分别导入最终报告。`review-status ID finding` 显示报告和门禁结果。补证或重审用 `review-start` 创建新的轮次，不去覆盖旧报告。
4. `approve ID --actor ... --plan ... --scope ...` 无回执时只展示 challenge/方案。操作者签署 challenge，以相同参数和 `--receipt human.json` 提交 `{challenge,signature}`。开始实施消耗一次授权；Issue 路径在首次创建本地草稿时消耗它。
4. PR/更新：实施、测试、提交本地代码，按步骤 2–3 双审 `diff`。`review-diff --approve` 仍可记录建议，但不能放行。Issue 不生成虚构的 diff。
5. 生成本地提交草稿后，双审 `submission`。提交包包含最终追加的追踪 marker。`approve-submit` 使用相同的挑战/签名流程，公开操作前重新验证双方回执、测试、目标、上游 SHA、查重以及人工授权，再进入 Outbox。Profile 使用 `profile` 对象与 `approve-profile`。当前不支持公开自动回复维护者。

报告签名绑定轮次、策略、初审摘要、仓库、版本和产物。测试日志、标题、正文或当前代码变化时，受影响的授权失效。仅改正文不用重跑不相关的代码测试。成功操作重复调用只读返回；远端结果不确定时仍只能对账，不重放。Portfolio 继续按真实上游采纳/合并证据入账。

## 数据与迁移

v1 → v2 在一个 SQLite 事务内增加 review_rounds、review_receipts、唯一约束和不可更新/删除触发器；不改写旧 Finding、审批或 Outbox，不把旧单审回填为 PASS。上线前停止写入并用 SQLite backup API 备份（不能只复制运行中的 WAL 主文件），先对副本运行新版。回滚保留 v2 文件，以备份另建 v1 实例；不要覆盖原件或对不确定的远端写入重试。
