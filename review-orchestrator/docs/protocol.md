# 操作协议

程序已经强制执行的规则，这里不再重复；本文只写需要人来决定的事项，以及程序做不到的地方。

## 人来决定

- **选关**：`--gates`。顺序和必经关由 `config/reviewers.json` 决定，GPT 与 Claude 为必经关。未列入的关卡不计票，也不算 PASS。
- **跳关**：只有非必经关可以跳。`gate.py skip <角色> operator <用户授权原话>`；Gemini 在已记录连接失败（`fail` 之后处于 blocked）时，可用 `skip gemini connection <证据>`。
- **盲审输入变更**：需求或核对说明必须改时，用 `--input-change=<理由>`。理由里不能写审阅意见。
- **证据分歧**：补丁不变时，实施者提交反证，用 `orchestrate.py recheck`。是否撤回由原审阅者决定，实施者不能自己关闭异议。
- **交付**：到 `ready` 后，由用户授权具体的补丁哈希和正文哈希（`authorize`），交付后记录回执（`complete`）。

## 中断与恢复

- 退出码 2（关卡已准备但未完成）：`gate.py fail <角色> <原因>` → `gate.py retry` → `orchestrate.py run`。
- BLOCKED：先看 `evidence/` 里的 `problems` 或 `transcripts/` 里的拒绝原因，修好环境后 `retry`、`run`。
- 新版本冻结时，上一版所有关卡的副本会自动封存到 `sealed/`。

## 程序做不到的地方

- 转录审计只能看到 CLI 输出里出现过的路径。Bash 可以用间接方式绕过，所以抽查时仍要读 `transcripts/`。需要真正的隔离时，用独立账号或容器运行 CLI。
- 模型回执依赖 CLI 输出里自带的型号字段。如果没有这个字段，记为 `ok: null`（未验证），不会因此拦截。
- 变异测试和程序重跑能证明"测试能识别这类错误"，不能证明"修复是对的"。后者始终由审阅者判断。
