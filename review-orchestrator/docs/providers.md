# 模型与接口接线

## 实施 GPT / 独立 GPT

OpenCode `vvvvvip/gpt-6.2-astra#high`，两个独立会话（型号与变体以 `config/reviewers.json` 为准）

OpenAI 兼容 `POST https://vvvvvip.tststs.shop/v1/chat/completions`

服务进程可见 `REVIEW_GPT_KEY`

## DeepSeek

OpenCode `opencode/deepseek-v4.1-flash-free`

[官方目录](https://opencode.ai/docs/zen)列出 `https://opencode.ai/inference/openai/v1/chat/completions`；免费型号限定经 OpenCode 调用

通过 OpenCode 接入提供方，检查额度及模型目录；不要用裸 HTTP 替代可实跑的审阅会话

## Gemini

OpenCode 自定义 `review-gemini/gemini-4-neon`

OpenAI 兼容 `POST https://api.zzshu.cc/v1/chat/completions`

`REVIEW_GEMINI_KEY`。旧部署对应变量是 `PETL_REVIEW_GEMINI_KEY`，此包使用通用名。曾用的备选为 `google/gemini-4-neon`（Google 原生；默认服务 `generativelanguage.googleapis.com`）以及 vvvvvip 兼容网关。确认同一型号可用后才能更换线路。

## Claude 独立终关

Claude Code CLI `claude-opus-5-5 --effort medium`

默认 Anthropic Messages 服务 `https://api.anthropic.com/v1/messages`；账号登录或企业代理时由 CLI 管理实际传输

Claude CLI 登录或 `ANTHROPIC_API_KEY`。若公司使用代理，按部署设置 `ANTHROPIC_BASE_URL`；不要给 Claude 配 OpenAI 的 `/chat/completions`。


兼容网关通常是 Bearer 鉴权。配置示例 `config/providers.example.json` 是 OpenCode V2 自定义 provider 片段：只合并其中的 `providers` 到**本机用户配置**，保留其他设置；不要复制到正式上游源码或覆盖现有配置。目录名前缀是本地路由名，斜杠后的模型才是请求型号。用 `opencode models` 和 `claude --help` 确认当前租户、CLI 和工具权限；地区、分组、额度都可能改变。`REVIEW_OPENCODE_BIN` / `REVIEW_CLAUDE_BIN` 可指定同一 CLI 的可执行名（Windows 可用 `opencode2.cmd`）。

只复制占位配置，不复制账号、API key、OAuth 缓存、个人配置、数据库或历史会话。普通 ping 成功不能代替工具执行；慢首字不等于失败。此工具不自动降级型号或购买额度。
