# 模型 token 上限与探索输出预算

2026-11-16（北京时间）核验官方文档与本地 Profile。上下文按单次请求的输入与输出合计预检，页面的 token 数是估算。表中“可留给输入”是预留模型最大输出时的理论差额，实际还要容纳系统指令、消息结构与安全余量。

| 官方 API 模型 | 上下文 tokens | 最大输出 tokens | 预留最大输出后可留给输入的 tokens |
|---|---:|---:|---:|
| DeepSeek V4.1 Flash（`deepseek-flash`，旧 `deepseek-v4-flash` 仍可用） | 1,048,576 | 393,216 | 655,360 |
| DeepSeek V4 Pro（`deepseek-v4-pro`） | 1,048,576 | 393,216 | 655,360 |
| Xiaomi MiMo V2.6 Flash、Pro | 1,048,576 | 131,072 | 917,504 |

DeepSeek 官方 `GET /models` 返回 HTTP 200，列出 `deepseek-flash` 和 `deepseek-v4-pro`，两者均返回精确上限 1,048,576 / 393,216。定价页说明旧 `deepseek-v4-flash` 仍接受请求，目前路由至 V4.1 Flash。Chat Completions 使用 `max_tokens`。 [模型列表](https://api-docs.deepseek.com/api/list-models/) · [模型与定价](https://api-docs.deepseek.com/quick_start/pricing/) · [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)

小米官方 OpenClaw 配置提供 MiMo Flash 和 Pro 的精确数值。官方 Chat Completions 使用 `max_completion_tokens`，上限 131,072，包含可见输出和推理。两条新增 Profile 使用按量 API。Token Plan 使用账户提供的独立端点和密钥。第三方代理按其实际配置与协议运行。 [模型目录](https://mimo.mi.com/docs/quick-start/model) · [精确配置值](https://mimo.mi.com/docs/en-US/tokenplan/integration/openclaw) · [Chat Completions](https://mimo.mi.com/docs/en-US/api/chat/openai-api-post) · [官方端点](https://mimo.mi.com/docs/tokenplan/subscription)

## 正式 Profile 变更

| ID | 请求模型 | 上下文 / 最大输出 | 状态 |
|---:|---|---|---|
| 1 | `deepseek-v4-flash` | 1,048,576 / 393,216 | 启用，保留原摘要默认。原上下文误填为 1,024,000,000 |
| 4 | `deepseek-flash` | 1,048,576 / 393,216 | 新增，显示名 DeepSeek V4.1 Flash，复用 #1 的凭据，接替全文默认 |
| 5 | `mimo-v2.6-flash` | 1,048,576 / 131,072 | 新增，按量 API，密钥空，停用 |
| 6 | `mimo-v2.6-pro` | 1,048,576 / 131,072 | 新增，按量 API，密钥空，停用 |

已删除 #2 `kimi-k2.6` 和 #3 `deepseek-v4-pro`。主库检查未发现对这两条 Profile 的 Prompt、评估、备忘录、调用账本或设置引用，任务和探索周报均为空。探索模型仍由页面或 Prompt 绑定选择。正式主库及 Profile 库的 SQLite 一致备份位于 `data/backups/pre_model_profiles_20261116_080837/`。备份及变更后数据库完整性均为 `ok`，外键错误为 0。

## 请求预算

- 探索按阶段请求输出。提取最多 1,000，汇总提高至 8,192，Profile 配置较低时采用较低值。每次汇总上限为三个结构化候选。能力达到 393,216 的模型仍按任务预算发请求。
- 生效预算随计划固化，预览、容量检查、实际请求和普通恢复使用同一个值。旧计划继续使用自己的预算，包括旧格式中固化的 2,000。明确换模型重试时，新的关联快照重新解析预算。
- 已核验端点按官方上下文上限与 Profile 上限中的较低值预检。DeepSeek 使用 `max_tokens`，官方 MiMo OpenAI 兼容端点使用 `max_completion_tokens`。第三方代理继续使用既有通用协议和实际配置。
- 推理模式可能在生成正文前消耗部分预算。汇总放宽后的截断率与提取 1,000 tokens 的质量仍需用真实论文和实际 Provider 响应确认。

## 验证

定向运行 `.\.venv\Scripts\python.exe -B -m unittest tests.test_exploration tests.test_llm` 共 46 项通过。通过实际 HTTP 客户端的模拟传输验证 DeepSeek 请求 8,192、新建计划采用 Profile 的较低上限、旧计划仍请求 2,000、MiMo 官方与代理使用不同字段。使用变更后的数据库副本验证 `/llm-profiles`、`/direction-exploration` 均返回 200，四条模型、默认规则和 MiMo 空密钥符合预期。真实 DeepSeek 请求仅查询模型元数据。生成请求、MiMo 密钥验证与实际 token 消耗均待运行验收。

最终 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` 执行 531 项，530 通过、1 跳过，耗时 87.541 秒。S6 兼容回退演练缺少 S4 匹配的 `db_before.py`。 `git diff --check` 通过。
