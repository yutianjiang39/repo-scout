# AI 审查：接口、配置与边界

## 已实现的接口

`providers.py` 提供独立的 OpenAI Responses、OpenAI-compatible Chat Completions、Anthropic Messages、Gemini generateContent HTTP 适配器。
核心审查逻辑只使用 `ModelRequest` / `ModelResponse`，
不选择某一家厂商或模型。`estimate_tokens` 是保守字节估算，
`get_usage` 返回服务商实际报告的数量；缺失数量保持 `null`。

本次核对的官方 API 文档：

- [OpenAI Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)：
  `/v1/responses`、`instructions` / `input`、文本输出及 usage。
- [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat)：
  兼容服务使用独立的 messages 结构，不假定其支持 Responses。
- [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create) 和 [API versioning](https://platform.claude.com/docs/en/api/versioning)：
  `/v1/messages`、独立 system、max_tokens 与版本头。
- [Gemini generateContent](https://aistudio.google.com/api-reference/generate-content/generate-content) 和 [response / usage reference](https://aistudio.google.com/api-reference/generate-content)：
  contents / parts、generationConfig、usageMetadata。

模型能力、可用性与价格以使用时的账户为准。示例配置不包含会被误当成真实模型 ID 发送的默认模型；
填写自己账户可用的模型 ID 后再启用角色。OpenAI `json_object` 为兼容 JSON 模式；
所有输出仍接受本地结构与源码位置验证。Anthropic 默认使用可配置的 `2023-06-01` 版本头，
JSON 由提示请求并本地验证；没有假设所有模型支持相同 JSON Schema 参数。

## 配置

```yaml
providers:
  chosen_api:
    type: openai                 # 也可 anthropic / gemini / openai_compatible
    api_key_env: OPENAI_API_KEY
    timeout_seconds: 90
    retries: 2
    retry_base_seconds: 1
    max_backoff_seconds: 30
    pricing: {}                  # 键为真实模型 ID；值为下列两个价格字段
    # MODEL_ID:
    #   input_per_million: <账户当前输入单价>
    #   output_per_million: <账户当前输出单价>
roles: {}                       # 配置真实 model 后再添加角色
audit:
  input_token_budget: 100000
  output_token_budget: 40000
  cost_budget_usd: null
  concurrency: 2
  max_chunks: 80
  context_tokens: 1500
  prompt_version: repo-scout-audit-v1
```

角色字段：`provider`、真实的 `model`、可选 `parameters`、`max_output_tokens`、`instruction`、`json_output`。
`task` 为 `audit`、`adversarial`、`synthesis` 或 `final_diff`；
常用的 `primary_auditor`、`independent_auditor`、`adversarial_reviewer`、`synthesis_reviewer`、`final_diff_reviewer` 可省略 task。
可以使用自定义角色名并显式指定 task。同一 Provider 不同模型的价格写在 `pricing` 的不同模型键下，
不会固定假定不同模型有同一价格。

只从环境变量读取密钥，程序不对配置文本作 shell 展开。模型 ID 本身直接从 YAML 字符串读取；
若希望通过 shell 生成配置，应在本地单独完成，不能把 `$MODEL` 占位符直接交给角色。
兼容服务可以设置 HTTPS `base_url`，以及 `max_tokens_field: max_tokens` 或 `max_completion_tokens`。
任何配置都不允许字面 `api_key`、`token` 或任意认证 headers。

## 缓存、预算与断点续跑

缓存键覆盖整个模型请求：源码及相邻上下文、system/prompt、prompt version、model、generation parameters、provider endpoint/version 和价格配置。
相同请求并发时只发一次；中途失败后重新运行会复用已经成功的请求。报告同时保存本次新增消耗和缓存命中标识，
历史 `model_usage` 与 `audit_run` 保留之前的已知消耗。

每次调用前按 UTF-8 字节数加框架余量预留输入，按最大输出预留输出，并为配置的全部重试次数预留预算。
这里的限制是保守的本地请求预算，不是服务商账单的强制额度；生产环境仍应设置服务商账户额度。
只有提供模型价格时才估算成本；设置货币预算却没有价格会在请求前拒绝执行。Anthropic 缓存 token 按配置输入价格估算，
未模拟所有缓存阶梯优惠；Gemini output 包含思想 token。实际账单可能因缓存、重试与供应商价格规则不同。

HTTP 429 / 部分 5xx 有上限重试并尊重 Retry-After；
超过最大等待时间则交给后续续跑。连接/读取超时存在未知收费结果，不自动重放。
发生重试时，只能观测成功响应的 usage，记录明确标记 `usage_complete: false`；
失败调用标记未知，而非计为零。缓存命中本次新增费用为零，原始响应 usage 仍留在缓存与历史记录。

## 数据与质量边界

- 原始仓库只读，`files/` 保存原路径和原字节，包括 CRLF。manifest、chunk 行号、内容哈希在审查前再次校验。
- Symlink、凭证文件、构建目录、二进制、大文件和含明显硬编码密钥的源码不会进入审查副本。
  秘密扫描是启发式，无法保证识别所有秘密；使用前应检查生成的 manifest。
- Python 通过 AST 尽量保持函数/类完整；超长函数才按行拆分并明确标记 partial。
  其他语言使用空行/行区间回退，不声称具有完整的 Rust/TS AST。
- 依赖、邻近源码、疑似调用者和测试以有限上下文补充。该静态映射不等于完整调用图；缺失上下文必须留给独立验证。
- 每个仓库最多五个 Finding，永远保持 `CANDIDATE`、`INFERRED`、`provenance: imported`。
  模型自称 `CONFIRMED`、`PASS` 不改变状态。不存在或未发送的位置会被拒绝。
- 反方角色可以提出反证，汇总角色可以排序；二者不能代替 CC 验证或用户审批。`review_diff` 也只有建议权限。
- 每个 chunk/task 的成功、失败、预算暂停写入 `verification.json`。
  有遗漏的批次标记 PARTIAL，不把缺乏输出说成全仓库无 Bug。
- 本版本是非流式文本审查，没有服务端 Batch API、远端 token-count API 或完整跨语言调用图。
  可通过现有 Provider 接口增量扩展。

## 独立测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_audit_bundle.py -v
```

所有测试使用本地 Git fixture 和 `httpx.MockTransport`，不需要真实 API Key，也不会提交上游内容。
