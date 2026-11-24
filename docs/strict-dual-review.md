# 本地双审与人工确认

无需服务器、密钥或签名服务。操作者将 GPT 和 Claude Code 的真实报告分别导入本地；角色是操作者记录的来源，程序不认证服务商身份。不要用模拟报告代替实际审查。

1. `review-start ID finding` 创建证据轮次；对最终代码、提交包和 Profile 分别使用 `diff`、`submission`、`profile`。
2. `review-packet ID finding reviewer_a independent` 导出 GPT 的材料，`reviewer_b` 用于 CC。两个新会话独立审查，不提供对方结论。保存每份报告为 UTF-8 JSON：`{"binding": <原 packet.binding>, "decision": <审查决定>}`。`decision` 包含 `session_id`、`verdict`、`evidence`（`kind` 和 `reference`）、`disagreements`、`rationale` 和 `reviewed_at`。使用 `review-import ID finding report.json` 分别导入。
3. 两份初审齐备后，用同样命令的 `final` 阶段给两个新会话提供双方意见。`review-status ID finding` 仅在两份终审均有证据、均为 PASS、没有未解分歧且产物仍匹配时显示 PASSED。
4. `approve`、`approve-submit`、`approve-profile` 默认只预览，返回 `approval_digest`。操作者核对后，以相同参数追加 `--confirm-digest <approval_digest>` 确认当前内容。`approve-submit` 的 `--digest` 指草稿摘要，与预览返回的审批摘要用途不同。实施和公开提交分别确认；验收不触发发送。

报告仍绑定当前轮次、源码、测试和草稿。内容变化须重新审查受影响的对象并取得相应确认。旧协议的历史记录不改写，也不自动算作新协议的 PASS。新轮次需重新取得实际报告，本地数据库只应由操作者写入。
