# Claude Code 集成

本程序把候选审查与独立工程验证分开。CC 验证只能针对当前上游 SHA 的代码和实际运行证据作判断；模型返回的 PASS 不会创建测试记录。

自动入口在一个空的临时目录调用已安装的 Claude Code CLI，使用 bare mode、空工具列表和空 MCP 配置，只通过 stdin 输入筛选后的上下文，独立解析 `structured_output` 并执行 Pydantic 校验。它不会读取旧会话，也不会执行仓库中的 hooks、配置或脚本。

验证使用 `ValidationReport` schema；修复阶段在已有方案批准后只要求 CC 生成 unified diff，再由程序进行文件范围检查和 Git apply。测试执行是另外一项经过检查的操作，公开写入是另外一项内容绑定的批准。

上下文有固定大小上限；需要更多调用链或依赖证据时应返回 BLOCKED，而不是猜测。外部 Claude Code 会话也能导入报告，但必须由操作人明确 `--attest-cc`，报告会保留 operator-import 来源，不能冒充内置 CLI 实际调用记录。

模型可用性和价格取决于用户自己的账户。`--max-budget-usd` 为 CC 客户端估计，可能与账单不同；还应使用服务商账户额度。没有 CLI 或 API Key 时程序保留任务包并明确报错，绝不伪造验证完成。

官方依据（本次实现时核对）：

- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)：`-p`、stdin、bare mode、JSON `structured_output`。
- [CLI reference](https://code.claude.com/docs/en/cli-reference)：`--tools`、`--disallowedTools`、`--strict-mcp-config`、`--setting-sources`、`--json-schema`、`--max-budget-usd`。

本次交付没有真实调用 Claude Code；可运行协议和报告校验有单元/集成测试，真实 CLI 版本、身份和计费需在部署环境确认。
