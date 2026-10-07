# LLM 默认并发与 provider 并发限流降档

日期：2026-10-07。

## 当前行为

- 默认 LLM 并发由 4 改为 10，配置、设置表单、界面回退值、每日计划、历史方向补评与摘要补评保持一致。用户保存的其他并发值仍作为初始上限，允许范围仍为 1–20。
- 设置值为 10 时，明确的 provider 并发限流触发 `10 → 4 → 3 → 2 → 1`。初始值大于 4 时首次降到 4；初始值不大于 4 时继续减 1；最低 1。
- 同一任务内不会因为一个成功响应而升回 10。分类与摘要阶段共用同一 provider 限额。下一项排队任务开始时，仅重置没有在途请求或等待者的 provider 为其配置上限；存在请求的 provider 不重置。设置变更通过新 HTTP client 注册，旧 client 不会覆盖新配置。
- 已发出的请求不取消。降低上限后，新请求等待旧请求结束，直到在途数低于新上限。
- 按 provider、HTTP origin 与认证信息指纹分组。同一账号的不同模型／不同客户端共享限额；不同账号和不同 provider 分开。认证信息只用于内存哈希，不写入日志或账本。
- 并发限流识别 HTTP 429/503 响应中的明确并发标识：`concurrent`、`concurrency`、`parallel requests`、`parallel_requests`、`并发`，或 `x-ratelimit-resource` 对应标识。普通 RPM/TPM、额度不足、笼统的 429/503 仍交由既有错误／重试机制处理。

## HTTP 请求与重试

闸门接入 `llm._tracked_post`，覆盖 OpenAI 兼容及 Anthropic 的分类、摘要、全文、补评、备忘录、连接测试及 response-format fallback。先取得并发许可，再登记 `provider_started`；排队期间不会被误记为已发出请求。

generation 标记每一档放行的请求。首个旧档并发错误降低一次限额，同一旧档随后返回的错误只按新档重试，不继续降低。只有新档实际放行的请求仍被并发限流，才会再降一档。

一次 `_tracked_post` 传输调用最多进行 5 次并发限流尝试，足以走完 10→4→3→2→1。每次物理请求独立登记调用账本，重试原因使用固定值 `provider_concurrency_limit`，不消耗外层业务重试次数。失败状态保持可追踪，没有吞掉或伪造成功响应。业务层仍有独立的有限重试预算；专项测试确认，持续并发限流且摘要额外重试为 2 时，首次业务调用发出 5 次请求，到并发 1 后的两次业务重试各发出 1 次，总计 7 次并以失败结束。

退避时释放并发许可，默认等待 1 秒；Retry-After 支持秒数及 HTTP 日期，等待限制在 1–30 秒，异常值回退为 1 秒。最低并发 1 仍限流时返回既有 LLMHTTPError，由业务层按已有有限重试规则处理，最终可能报告失败，不无限重试。

无论 provider 传输失败、请求开始登记失败或响应登记失败，都释放许可。已收到并发错误但响应写库失败时，仍保留降档状态，同时向上抛出数据库异常，不隐式再发请求。

## 验证

专项测试覆盖默认值、在途请求排空、旧 generation 去重、账号隔离、10→4→3→2→1、并发 4 的 provider 阈值、逐次物理账本、RPM 与并发限流区分、Anthropic 最低并发仍失败、传输/数据库异常释放，以及分类→摘要保持 4、下一任务恢复配置上限。

专项测试 36 项已通过，包含设置默认值与异常回退。隔离 smoke 完成 100 篇分类、20 篇摘要，同日重跑零新增 LLM 调用。正式本地并发设置已从 4 更新为 10；其他保存值未更新。没有自动重启应用；`run.py` 关闭热重载，运行中的应用需要重启才能加载新的闸门逻辑。

## 交接

- `cwd`：`E:\claude-projects\daily-coolpapers`；按用户提供的 AGENTS 指令工作，磁盘无 AGENTS.md。
- Goal：默认 LLM 并发设为 10，provider 并发限流时动态降档。
- Files read：LLM、调用账本、任务调度、服务、配置、设置表单及相关测试。
- Files changed：新增 `llm_concurrency.py`、`test_llm_concurrency.py`、本文及隔离 smoke 结果；修改 llm、call_attempts、jobs、services、config、form_commands、app、settings 模板、settings 测试及 pipeline 基准工具。本地仅更新并发设置。
- Commands run：专项 unittest；完整 unittest；隔离 pipeline smoke；本地单一并发设置更新。
- Validation result：专项 36 项通过，隔离 smoke 通过；最终完整回归 493 项，492 通过、1 跳过（79.553 秒），跳过原因为缺少旧 S4 `db_before.py` 夹具；`git diff --check` 通过。模拟测试与 smoke 没有真实 LLM 调用。
- Unverified areas：真实 provider 并发限流格式；持续 RPM/TPM 配额限制；跨多个应用进程的全局限额。此闸门只协调当前应用进程。
- Key decisions：任务内不自动升档；新任务空闲时恢复配置上限；独立记录每次物理请求；不修改其他业务数据，不调用付费模型。
- Recommended next prompt：用真实 provider 小样本验证并发 10 的吞吐和限流识别。
