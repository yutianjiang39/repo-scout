# 本地三审与人工确认

默认由 GPT (`reviewer_a`)、Claude Code (`reviewer_b`，`claude-opus-5-5`、medium) 和 `gemini-4-neon` (`reviewer_c`) 三审。无需签名服务。手动导入的角色是操作者记录的来源，不证明供应商身份。

1. `review-start ID finding` 创建证据轮次；对最终代码、提交包和 Profile 分别使用 `diff`、`submission`、`profile`。
2. `review-packet ID finding reviewer_a independent` 导出指定角色材料，每个模型使用新会话独立审查。保存每份报告为 UTF-8 JSON：`{"binding": <原 packet.binding>, "decision": <审查决定>}`。`decision` 包含 `session_id`、`verdict`、`evidence`（`kind` 和 `reference`）、`disagreements`、`rationale` 和 `reviewed_at`。用 `review-import ID finding report.json` 导入。Gemini 也可直接调用：设置环境变量 `REPO_SCOUT_GEMINI_API_KEY`，执行 `review-gemini ID finding independent`，经 `/v1/chat/completions` 发起一次付费请求并导入结果。
3. 所有参与者的初审齐备后，用 `final` 阶段提供全部已记录的初审意见。所有参与者都要给出有证据的 PASS 且无未解分歧。网络确实故障时，操作者可执行 `review-network-failure ID finding reviewer_c timeout`（角色可换成任意一位）；本轮保留故障记录，改由另外两位完成。不得因负面意见、缺凭证或格式错误降级。终审已有结论或初审存在异议的角色不能被排除；一轮最多排除一位。`review-status` 显示参与者、降级原因和报告。
4. `approve`、`approve-submit`、`approve-profile` 默认只预览，返回 `approval_digest`。操作者核对后，以相同参数追加 `--confirm-digest <approval_digest>` 确认当前内容。`approve-submit` 的 `--digest` 指草稿摘要，与预览返回的审批摘要用途不同。实施和公开提交分别确认；验收不触发发送。

报告仍绑定当前轮次、源码、测试和草稿。内容变化须重新审查受影响的对象并取得相应确认。旧协议的历史记录不改写，也不自动算作新协议的 PASS。新轮次需重新取得实际报告，本地数据库只应由操作者写入。
