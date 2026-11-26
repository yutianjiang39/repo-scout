# 一句话部署

把完整文件夹通过私有渠道复制到新项目（包括 `private/credentials.json`），打开新项目里的 AI，说：**请按照这个文件夹中的内容部署流水线。**

给部署 AI：在包根目录（本文件上一级）直接运行 `python -B deploy.py`。脚本会检查 Git、Python、OpenCode V2、Claude CLI、当前四个模型入口、本地私有配置，并实跑门禁测试；成功生成 `.runtime/deployment.json`。只在本文件夹内落地，不覆盖新项目代码或用户全局配置。运行器会自动加载本包凭证，无须复制密钥到环境变量或聊天。缺工具时按目标平台安装缺失依赖再运行；无法安装或权限不足则报告具体阻塞，不能把失败说成成功。

## 私有版接线

- GPT：`https://vvvvvip.tststs.shop/v1/chat/completions`，使用随包的该网关密钥。
- Claude：`https://vvvvvip.tststs.shop/v1/messages`（CLI 的 `ANTHROPIC_BASE_URL` 为 `https://vvvvvip.tststs.shop/`，配 `ANTHROPIC_AUTH_TOKEN`）。
- Gemini：本机没有可取得的 zzshu 专用密钥，私有包配置为同一 vvvvvip 网关的 `gemini-4-neon`，仍是原型号。此前该线路曾返回分组不可用；本地部署成功不代表供应商已恢复。若拿到 zzshu 的密钥，可由操作者在私有文件同时替换 Gemini 的 `baseURL` 为 `https://api.zzshu.cc/v1` 及其 `key`，不要跨域复用密钥。
- DeepSeek：OpenCode 托管免费模型，`https://opencode.ai/inference/openai/v1/chat/completions`；通过 CLI 使用，当前包没有可导出的独立 DeepSeek 密钥。额度与平台账号不是复制文件可以迁移的东西。

`private/` 含真实明文凭证，已在本包 `.gitignore` 中排除；普通 git clone 不会带上它。只通过可信私有渠道复制，勿放进公开仓库、PR、日志或模型提示词。其他平台复制后的文件权限可能不同。启动器只为当前角色注入对应的随包配置。本机未在线验证全部模型；部署脚本不花额度，也不产生审阅票。
