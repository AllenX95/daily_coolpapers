# Daily Cool Papers PRD

## 1. 文档信息

- 产品名称：Daily Cool Papers
- 文档类型：产品需求文档
- 目标版本：MVP v0.1
- 运行方式：本地轻量 Web 服务，浏览器访问
- 目标用户：需要每日跟踪 arXiv / papers.cool 热门论文，并用 LLM 做初筛和深度阅读的研究者、投资人、工程团队成员

## 2. 背景

papers.cool 每日同步 arXiv 更新，并在多个论文类目下展示论文列表。每个类目页面支持通过 `sort=1` 按 reading stars 从高到低排序，例如：

```text
https://papers.cool/arxiv/cs.AI?sort=1
```

用户希望构建一个轻量工具，每天自动抓取不同类目下 reading stars 排名前 30 的论文，先通过论文标题和 Abstract 让 LLM 初步评估，再在用户主动点击时下载 PDF、使用 MarkItDown 转成 Markdown，并将全文一次性提交给 LLM 阅读和总结。

该工具需要支持 OpenAI 和 Anthropic 两种 API 格式，支持 Prompt 管理、模型配置、缓存清理、日志查看，并能从 VC 投资视角评估论文潜在影响。

## 3. 产品目标

### 3.1 核心目标

1. 每日自动抓取指定类目的 papers.cool top30 论文。
2. 用 LLM 基于标题和 Abstract 快速评估论文价值。
3. 用户点击按钮后，再触发 PDF 下载、MarkItDown 转 Markdown、全文 LLM 阅读。
4. 支持独立配置 LLM Profile、Prompt 和模型绑定。
5. 提供浏览器访问的本地 Web UI，不开发独立桌面 GUI。
6. 保留本次运行日志，服务启动时清空上次日志，方便开发调试。

### 3.2 非目标

1. 不做多人协作、账号系统和云端同步。
2. 不保证 papers.cool HTML 结构变化后的自动兼容，但解析器需易于维护。
3. 不在 MVP 中实现复杂推荐系统或长期个性化排序。
4. 不在 MVP 中强制接入 arXiv API，除非需要补充元数据。
5. 不对全文做自动分块。全文分析时整篇 Markdown 一次性传入 LLM。

## 4. 用户场景

### 4.1 每日快速筛选

用户打开本地服务，查看当天不同类目的 top30 论文。系统已经抓取论文标题、Abstract、作者、发布时间、subjects、reading stars 和 PDF 链接，并完成摘要级 LLM 评估。用户可按分数、类目、标签、VC 价值筛选。

### 4.2 深度阅读某篇论文

用户在列表中发现一篇值得关注的论文，点击“全文阅读”。系统下载 PDF，缓存 PDF 文件，用 MarkItDown 转 Markdown，再将整篇 Markdown 提交给指定 LLM Prompt，返回全文总结、技术判断、局限性和 VC 投资视角分析。

### 4.3 调整 Prompt 和模型

用户认为默认 Prompt 不适合自己的偏好，可以在 Prompt 管理页编辑默认 Prompt，或新增 Prompt。每个 Prompt 可绑定不同 LLM Profile，例如摘要评估用便宜模型，全文阅读用长上下文模型。

### 4.4 Debug

用户开发或运行时遇到抓取失败、LLM 返回异常、PDF 转换失败，可以打开日志页查看当前服务运行日志。服务每次启动会清空上次日志，保证日志只反映当前运行。

## 5. 产品形态

### 5.1 技术形态

推荐技术栈：

- 后端：Python + FastAPI
- 前端：Server-rendered HTML + HTMX 或 Alpine.js
- 数据库：SQLite
- 任务执行：FastAPI BackgroundTasks / APScheduler / lightweight job queue
- HTML 解析：httpx + selectolax 或 BeautifulSoup
- PDF 转 Markdown：MarkItDown
- 配置：SQLite + `.env` + 本地加密存储
- 日志：Python logging 写入 `logs/current.log`

### 5.2 运行方式

启动命令示例：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

浏览器访问：

```text
http://127.0.0.1:8765
```

## 6. 功能需求

## 6.1 类目管理

### 需求

用户可以配置需要抓取的 papers.cool arXiv 类目。

### 字段

- 类目 ID，例如 `cs.AI`
- 显示名称，例如 `Artificial Intelligence`
- 是否启用
- 每日抓取数量，默认 `30`
- 排序参数，默认 `sort=1`

### 验收标准

- 用户可以新增、编辑、停用类目。
- 抓取任务只处理启用类目。
- 默认每个类目抓取 top30。

## 6.2 论文抓取

### 需求

系统从 papers.cool 抓取每个类目按 reading stars 排序后的 top30 论文。

### 抓取 URL

基础格式：

```text
https://papers.cool/arxiv/{category}?sort=1
```

实现时可探测是否支持 `show=50` 或 `show=100` 等参数，用于减少分页或截断风险。

### 抓取字段

- arXiv ID
- 标题
- 作者
- Abstract
- Subjects
- 发布时间
- 类目
- 类目内排名
- Reading stars
- PDF URL
- papers.cool URL
- arXiv abs URL
- 抓取时间

### 抓取策略

- 使用普通 HTTP 请求和 HTML 解析，不使用浏览器自动化。
- 并发数默认 4-8。
- 请求带 User-Agent。
- 设置超时、重试和错误日志。
- 按 `arxiv_id` 去重。
- 同一论文出现在多个类目时，保留多个类目排名关系。

### 验收标准

- 单个类目能稳定获取 top30。
- 多个类目抓取时不会重复创建论文记录。
- 抓取失败时不影响其他类目。
- 错误写入日志和任务状态。

## 6.3 摘要级 LLM 评估

### 需求

系统基于论文标题、Abstract、subjects、发布时间、类目排名和 reading stars 调用 LLM，生成初步评价。

### 触发方式

- 抓取后自动触发。
- UI 中支持手动重新评估。
- 支持选择 Prompt 和 LLM Profile。

### 输出要求

输出结构化 JSON，包含：

- 评分
- 是否值得阅读
- 中文摘要
- 核心想法
- 新颖性
- 实用价值
- 技术深度
- 风险和局限
- 推荐下一步
- 标签
- VC 投资视角

### 验收标准

- 每篇论文可保存多个 Prompt / 模型版本的评估结果。
- LLM 输出 JSON 解析失败时保存原始文本，并标记失败。
- 用户可以重新评估并保留历史版本。

## 6.4 全文阅读

### 需求

全文阅读必须由用户点击按钮触发，不在抓取阶段自动执行。

### 流程

1. 用户点击论文卡片或表格行中的“全文阅读”按钮。
2. 系统检查是否已有 Markdown 缓存。
3. 若无 Markdown，检查是否已有 PDF 缓存。
4. 若无 PDF，下载 PDF。
5. 使用 MarkItDown 将 PDF 转换为 Markdown。
6. 检查 Markdown 估算 token 是否超过所选模型上下文。
7. 若未超过，将整篇 Markdown 一次性传入 LLM。
8. 保存全文评估结果。

### 约束

- 不做自动分块。
- 超过模型上下文时，提示用户更换长上下文模型或取消任务。
- 只允许下载可信 PDF URL，默认允许 arXiv PDF 链接。

### 验收标准

- 用户点击后才下载 PDF 和转换 Markdown。
- PDF、Markdown 可复用缓存。
- 全文评估结果和摘要评估结果分开存储。
- MarkItDown 转换失败时显示错误状态并写入日志。

## 6.5 MarkItDown 集成

### 需求

使用 MarkItDown 将论文 PDF 转为适合 LLM 输入的 Markdown。

### 实现建议

Python API 示例：

```python
from markitdown import MarkItDown

md = MarkItDown()
result = md.convert(local_pdf_path)
markdown = result.text_content
```

### 安全要求

- 不直接对任意用户输入 URL 调用 MarkItDown。
- 先下载到本地缓存，再对本地文件转换。
- 默认只允许 arXiv PDF 域名。

## 6.6 Prompt 管理

### 需求

系统内置默认 Prompt，同时允许用户手动编辑、新增、复制、停用 Prompt。

### Prompt 类型

- `abstract_review`：摘要级评估
- `fulltext_review`：全文级评估

### 字段

- Prompt ID
- 名称
- 类型
- 模板内容
- 绑定 LLM Profile
- 是否默认
- 是否启用
- 版本号
- 创建时间
- 更新时间

### 模板变量

摘要 Prompt 支持：

- `{{title}}`
- `{{category}}`
- `{{rank}}`
- `{{stars}}`
- `{{published_at}}`
- `{{subjects}}`
- `{{abstract}}`

全文 Prompt 额外支持：

- `{{markdown}}`

### 验收标准

- 用户可以编辑内置 Prompt。
- 用户可以新增 Prompt。
- 每个 Prompt 可以选择不同 LLM Profile。
- 历史评估结果记录使用的 Prompt 版本和模型。

## 6.7 默认摘要 Prompt

```text
你是一名严谨的科研论文筛选助手。请只根据论文标题、摘要、类别、发布时间和榜单排序信息，判断这篇论文是否值得进一步阅读。

评估目标：
1. 判断论文的核心问题、方法和可能贡献。
2. 识别它是否可能具有新颖性、实用价值或后续研究价值。
3. 从 VC 投资视角评估：这项研究是否可能改变某个市场、基础设施层、应用场景或创业机会。
4. 区分真实商业潜力和纯学术价值，不要把技术新颖性直接等同于可投资性。
5. 不要夸大摘要中没有明确支持的结论。
6. 如果信息不足，请明确说明不确定性。

请输出 JSON：
{
  "score": 0-100,
  "attention": "must_read | read | skim | ignore",
  "summary_zh": "中文三句话以内总结",
  "core_idea": "一句话说明论文核心想法",
  "why_interesting": ["值得关注的原因"],
  "novelty": 0-10,
  "practical_value": 0-10,
  "technical_depth": 0-10,
  "risk_or_limitations": ["潜在问题或不确定性"],
  "recommended_next_step": "是否建议全文阅读，以及原因",
  "vc_perspective": {
    "impact": "从VC投资视角看，这篇论文可能带来的影响",
    "market_relevance": 0-10,
    "commercialization_path": "潜在商业化路径，若不明显则说明原因",
    "startup_opportunities": ["可能关联的创业机会"],
    "investment_risks": ["投资视角下的风险或不确定性"]
  },
  "tags": ["关键词"]
}

输入论文信息：
标题：{{title}}
类别：{{category}}
排名：{{rank}}
Reading stars：{{stars}}
发布时间：{{published_at}}
Subjects：{{subjects}}
摘要：
{{abstract}}
```

## 6.8 默认全文 Prompt

```text
你是一名科研论文阅读助手。请阅读下面由 PDF 转换得到的 Markdown 全文，并给出面向研究人员和 VC 投资视角的评估。Markdown 可能存在公式、表格或参考文献转换不完整的问题，请基于可读内容谨慎判断。

评估目标：
1. 总结论文解决的问题、核心方法、实验设置和主要结论。
2. 判断论文相对已有工作的可能增量。
3. 评估是否值得后续深入阅读、复现或跟踪。
4. 从 VC 投资视角评估：这项研究是否可能改变某个市场、基础设施层、应用场景或创业机会。
5. 区分真实商业潜力和纯学术价值，不要把技术新颖性直接等同于可投资性。
6. 明确指出证据不足、实验缺口、假设限制或潜在风险。
7. 不要编造全文中没有的信息。

请输出 JSON：
{
  "score": 0-100,
  "attention": "must_read | read | skim | ignore",
  "one_sentence_summary": "一句话总结",
  "detailed_summary_zh": "较完整中文总结",
  "problem": "论文要解决的问题",
  "method": "核心方法",
  "experiments": "实验和评估方式",
  "main_findings": ["主要发现"],
  "novelty_assessment": "新颖性判断",
  "strengths": ["优点"],
  "weaknesses": ["缺点或限制"],
  "reproduction_value": 0-10,
  "follow_up_questions": ["后续值得追问的问题"],
  "recommended_action": "下一步建议",
  "vc_perspective": {
    "impact": "从VC投资视角看，这篇论文可能带来的影响",
    "market_relevance": 0-10,
    "commercialization_path": "潜在商业化路径",
    "startup_opportunities": ["可能关联的创业机会"],
    "investment_risks": ["投资视角下的风险或不确定性"],
    "time_to_market": "短期 | 中期 | 长期 | 不明确"
  },
  "tags": ["关键词"]
}

论文元信息：
标题：{{title}}
类别：{{category}}
摘要：
{{abstract}}

论文全文 Markdown：
{{markdown}}
```

## 6.9 LLM Profile 管理

### 需求

系统支持 OpenAI 和 Anthropic 两种 API 格式，并允许配置多个模型档案。

### Provider 类型

- `openai_compatible`
- `anthropic`

### 字段

- Profile ID
- 显示名称
- Provider 类型
- Base URL
- Model
- API key 存储引用
- 自定义 headers
- Temperature
- Max output tokens
- Context window tokens
- Timeout seconds
- 是否启用

### API key 本地加密

优先级：

1. Windows 使用 DPAPI / Credential Manager。
2. 跨平台使用系统 keyring。
3. 备用方案使用 `cryptography.Fernet`，主密钥保存在本机用户目录下。

### UI 要求

- API key 输入后加密保存。
- 展示时只显示掩码，例如 `sk-...abcd`。
- 支持测试连接。
- 支持设置默认摘要模型和默认全文模型。

### 验收标准

- OpenAI 格式接口可成功调用 `/v1/chat/completions`。
- Anthropic 格式接口可成功调用 Messages API。
- 不以明文形式存储 API key。

## 6.10 缓存管理

### 需求

PDF 和 Markdown 分开缓存，并支持独立保留天数。

### 默认配置

```yaml
cache:
  pdf_retention_days: 5
  markdown_retention_days: 7
  cleanup_on_start: true
  cleanup_daily: true
```

### 缓存目录

```text
cache/
  pdf/
    {arxiv_id}.pdf
  markdown/
    {arxiv_id}.md
```

### 清理策略

- 服务启动时执行一次清理。
- 每天定时执行一次清理。
- PDF 默认删除超过 5 天未访问或未修改的文件。
- Markdown 默认删除超过 7 天未访问或未修改的文件。
- 评估结果保存在 SQLite，不随缓存删除。

### 验收标准

- 用户可在设置页修改 PDF 和 Markdown 保留天数。
- 清理操作写入日志。
- Markdown 缓存被删除后，再次全文阅读可重新生成。

## 6.11 日志管理

### 需求

系统保留每次运行日志，服务每次打开后清空上次日志，方便开发后 debug。

### 日志文件

```text
logs/current.log
```

### 策略

- 服务启动时清空 `logs/current.log`。
- 日志写入抓取、LLM 调用、PDF 下载、MarkItDown 转换、缓存清理、异常堆栈。
- UI 提供日志页，支持刷新和下载当前日志。

### 日志级别

- `INFO`：任务开始、任务完成、清理统计
- `WARNING`：字段缺失、LLM JSON 解析失败、缓存不存在
- `ERROR`：抓取失败、LLM 调用失败、PDF 下载失败、MarkItDown 失败

### 验收标准

- 重启服务后旧日志被清空。
- 当前运行过程中的关键操作可在 UI 日志页看到。
- 异常包含可定位的错误信息。

## 6.12 Web UI

### 页面列表

1. 首页 / 今日论文
2. 论文详情页
3. Prompt 管理页
4. LLM Profile 管理页
5. 类目管理页
6. 缓存与系统设置页
7. 日志页

### 首页功能

- 按日期筛选
- 按类目筛选
- 按 LLM 评分排序
- 按 reading stars 排序
- 按 attention 筛选
- 显示摘要评估状态
- 显示全文评估状态
- 支持批量重新摘要评估

### 论文卡片 / 表格字段

- 类目排名
- Reading stars
- 标题
- 作者
- Abstract 摘要折叠展示
- LLM score
- attention
- 中文简评
- VC impact 简评
- 标签
- 操作按钮：
  - 重新摘要评估
  - 全文阅读
  - 查看 Markdown
  - 打开 PDF
  - 打开 arXiv

### 论文详情页

- 基础元信息
- 摘要级评估历史
- 全文级评估历史
- Markdown 缓存状态
- PDF 缓存状态
- 任务日志摘要

## 7. 数据模型

## 7.1 `papers`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| arxiv_id | text | arXiv ID，唯一 |
| title | text | 标题 |
| authors | text/json | 作者 |
| abstract | text | 摘要 |
| subjects | text/json | subjects |
| published_at | datetime | 发布时间 |
| pdf_url | text | PDF URL |
| abs_url | text | arXiv abs URL |
| papers_cool_url | text | papers.cool URL |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

## 7.2 `paper_categories`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| paper_id | integer | 论文 ID |
| category | text | 类目 |
| crawl_date | date | 抓取日期 |
| rank | integer | 类目内排名 |
| reading_stars | integer | reading stars |
| created_at | datetime | 创建时间 |

## 7.3 `llm_profiles`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| name | text | 显示名称 |
| provider | text | `openai_compatible` / `anthropic` |
| base_url | text | API base URL |
| model | text | 模型名称 |
| encrypted_api_key_ref | text | 加密 key 引用 |
| temperature | real | temperature |
| max_output_tokens | integer | 最大输出 token |
| context_window_tokens | integer | 上下文窗口 |
| timeout_seconds | integer | 超时 |
| enabled | boolean | 是否启用 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

## 7.4 `prompts`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| name | text | Prompt 名称 |
| type | text | `abstract_review` / `fulltext_review` |
| template | text | Prompt 模板 |
| llm_profile_id | integer | 绑定模型配置 |
| version | integer | 版本号 |
| is_default | boolean | 是否默认 |
| enabled | boolean | 是否启用 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

## 7.5 `evaluations`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| paper_id | integer | 论文 ID |
| evaluation_type | text | 摘要 / 全文 |
| prompt_id | integer | Prompt ID |
| prompt_version | integer | Prompt 版本 |
| llm_profile_id | integer | LLM Profile |
| model | text | 实际调用模型 |
| status | text | success / failed |
| result_json | text/json | 结构化结果 |
| raw_output | text | 原始输出 |
| error_message | text | 错误 |
| created_at | datetime | 创建时间 |

## 7.6 `jobs`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | integer | 主键 |
| type | text | crawl / abstract_eval / fulltext_eval / cleanup |
| status | text | pending / running / success / failed |
| payload | text/json | 任务参数 |
| error_message | text | 错误 |
| started_at | datetime | 开始时间 |
| finished_at | datetime | 结束时间 |

## 7.7 `settings`

| 字段 | 类型 | 说明 |
|---|---|---|
| key | text | 设置项 |
| value | text/json | 设置值 |
| updated_at | datetime | 更新时间 |

## 8. 任务流程

### 8.1 每日抓取流程

```text
读取启用类目
  -> 构造 papers.cool URL
  -> 并发抓取 HTML
  -> 解析论文列表
  -> 保存 papers 和 paper_categories
  -> 为新增或更新论文创建摘要评估任务
  -> 写入日志和任务状态
```

### 8.2 摘要评估流程

```text
读取论文元信息
  -> 读取默认 abstract_review Prompt
  -> 渲染 Prompt 模板
  -> 调用绑定 LLM Profile
  -> 解析 JSON
  -> 保存 evaluations
  -> 更新 UI 状态
```

### 8.3 全文评估流程

```text
用户点击全文阅读
  -> 检查 Markdown 缓存
  -> 检查 PDF 缓存
  -> 下载 PDF
  -> MarkItDown 转 Markdown
  -> 估算 token
  -> 若超过上下文，提示用户更换模型
  -> 渲染 fulltext_review Prompt
  -> 调用 LLM
  -> 保存 evaluations
```

## 9. API 设计

### 页面路由

- `GET /`
- `GET /papers/{paper_id}`
- `GET /prompts`
- `GET /llm-profiles`
- `GET /categories`
- `GET /settings`
- `GET /logs`

### 操作接口

- `POST /api/crawl/run`
- `POST /api/papers/{paper_id}/evaluate-abstract`
- `POST /api/papers/{paper_id}/evaluate-fulltext`
- `POST /api/prompts`
- `PUT /api/prompts/{prompt_id}`
- `POST /api/llm-profiles`
- `PUT /api/llm-profiles/{profile_id}`
- `POST /api/llm-profiles/{profile_id}/test`
- `POST /api/cache/cleanup`
- `GET /api/logs/current`

## 10. 配置示例

```yaml
server:
  host: 127.0.0.1
  port: 8765

crawler:
  default_top_n: 30
  concurrency: 6
  timeout_seconds: 20
  retries: 2
  user_agent: "DailyCoolPapers/0.1"

cache:
  pdf_retention_days: 5
  markdown_retention_days: 7
  cleanup_on_start: true
  cleanup_daily: true

logs:
  current_log_path: "logs/current.log"
  clear_on_start: true
```

## 11. 非功能需求

### 11.1 轻量易用

- 单进程本地服务。
- SQLite 本地数据库。
- 浏览器访问，无独立 GUI。
- 初始配置尽量少。

### 11.2 抓取效率

- 多类目并发抓取。
- 只抓 HTML，不自动下载 PDF。
- PDF 和 Markdown 仅按需生成。

### 11.3 可维护性

- papers.cool 解析逻辑独立模块化。
- Prompt 模板和模型配置不写死。
- LLM Provider 抽象统一。

### 11.4 隐私与安全

- API key 本地加密保存。
- 不上传 PDF 或 Markdown 到非用户配置的 LLM 服务。
- 限制 PDF 下载域名。
- 日志避免打印完整 API key。

### 11.5 稳定性

- 单篇失败不影响批量任务。
- 任务状态可追踪。
- 错误可在 UI 和日志中查看。

## 12. MVP 验收标准

1. 用户能启动本地 Web 服务并打开浏览器页面。
2. 用户能配置至少一个 papers.cool 类目。
3. 系统能抓取该类目 `sort=1` 下 top30 论文。
4. 系统能保存论文基础信息到 SQLite。
5. 用户能配置 OpenAI-compatible 和 Anthropic LLM Profile。
6. API key 不以明文保存。
7. 系统内置摘要 Prompt 和全文 Prompt。
8. 用户能编辑和新增 Prompt，并为 Prompt 选择模型。
9. 系统能对论文 Abstract 执行 LLM 评估并展示结果。
10. 用户点击按钮后，系统才下载 PDF、转换 Markdown 和执行全文评估。
11. 全文 Markdown 整篇一次性传入 LLM，不自动分块。
12. PDF 缓存默认 5 天清理。
13. Markdown 缓存默认 7 天清理。
14. 服务启动时清空 `logs/current.log`。
15. UI 可查看当前运行日志。

## 13. 风险与应对

### 13.1 papers.cool HTML 结构变化

风险：站点没有正式 API，HTML 结构变化会导致解析失败。

应对：

- 将解析器独立封装。
- 保存解析失败的 HTML 片段用于 debug。
- 为典型页面建立快照测试。

### 13.2 LLM 输出不是合法 JSON

风险：模型可能输出非 JSON 文本。

应对：

- Prompt 明确要求 JSON。
- JSON 解析失败时保存 raw output。
- UI 标记失败并支持重试。

### 13.3 全文超出模型上下文

风险：Markdown 全文可能超过所选模型上下文。

应对：

- 调用前估算 token。
- 不自动分块。
- 提示用户更换长上下文模型或取消。

### 13.4 PDF 转 Markdown 质量不稳定

风险：公式、表格、图像说明可能转换不完整。

应对：

- Prompt 中说明 Markdown 可能不完整。
- 保留 Markdown 查看入口。
- 日志记录转换状态。

### 13.5 API key 泄露

风险：日志、数据库或 UI 暴露 API key。

应对：

- API key 加密保存。
- 日志脱敏。
- UI 只显示掩码。

## 14. 开发里程碑

### Milestone 1：基础服务与抓取

- FastAPI 项目骨架
- SQLite schema
- 类目管理
- papers.cool 抓取 top30
- 首页论文列表

### Milestone 2：LLM 和 Prompt

- LLM Profile 管理
- API key 加密保存
- Prompt 管理
- 默认摘要 Prompt
- 摘要级 LLM 评估

### Milestone 3：全文阅读

- PDF 下载和缓存
- MarkItDown 转 Markdown
- 默认全文 Prompt
- 全文 LLM 评估
- 上下文长度检查

### Milestone 4：运维能力

- PDF 5 天清理
- Markdown 7 天清理
- 启动清空日志
- 日志 UI
- 任务状态页或任务状态组件

### Milestone 5：打磨

- 筛选和排序
- 导出 Markdown / CSV
- 解析器测试
- LLM 输出解析测试
- 基础文档

## 15. 后续可扩展方向

1. 接入 arXiv API 补充标准元数据。
2. 增加用户自定义评分维度。
3. 增加长期论文跟踪和相似论文聚类。
4. 支持按主题订阅和每日邮件摘要。
5. 支持更多 LLM Provider。
6. 支持导出到 Notion、Obsidian 或 Markdown 知识库。
