# Daily Cool Papers

一个轻量、本地优先的 Web 服务，帮助早期 VC 投资人和科研人员跟踪并消化 [papers.cool](https://papers.cool) 上的 arXiv 论文。项目以 **papers.cool 的阅读星星数**（PDF 点击 + Kimi 点击）作为众包质量信号，再利用大语言模型从 VC 投资视角评估摘要，并在需要时按需精读全文。

[![GitHub](https://img.shields.io/badge/GitHub-AllenX95%2Fdaily__coolpapers-blue)](https://github.com/AllenX95/daily_coolpapers)

---

## 它能做什么

1. **每日抓取热门论文**：针对可配置的 arXiv 类目（CS.AI、CS.CL、CS.CV、CS.LG 等），抓取 `https://papers.cool/arxiv/{category}?sort=1` 的 top N 论文。
2. **捕获人气信号**：排名、PDF 点击数、Kimi 点击数，以及合并后的 **reading stars（阅读星星数）**。
3. **本地存储所有数据**：使用 SQLite，让你可以离线浏览、检索和过滤历史论文。
4. **LLM 摘要评估**：使用针对 VC 风格筛选调优的 Prompt，输出评分、关注等级、新颖性、实用价值、市场相关性、创业机会和投资风险。
5. **按需全文精读**：下载 PDF，将其转换为 Markdown（优先 MarkItDown，回退 pdfminer），再对完整论文运行第二轮 LLM 评估。
6. **定时运行**：每天在配置的时间自动抓取并清理缓存。

---

## 核心特性

- **本地 Web UI**：基于 Flask + Jinja2 模板。
- **无需浏览器自动化**：仅使用普通 HTTP + HTML 解析，轻量且稳定。
- **类目管理**：启用/禁用类目，配置每个类目的 `top_n` 和排序参数。
- **Prompt 引擎**：编辑、复制 Prompt，并将不同 Prompt 绑定到不同 LLM 模型；Prompt 支持 `{{变量}}` 替换。
- **LLM Profile 管理器**：支持 OpenAI-compatible API 和 Anthropic Messages API；API key 本地加密存储（Windows 优先 DPAPI，否则 Fernet）。
- **任务队列**：后台工作线程处理抓取、补抓、摘要评估、全文阅读和缓存清理，并显示实时进度。
- **导出功能**：支持 CSV 批量导出，以及包含所有评估记录的 Markdown 单篇导出。
- **收藏夹**：记录你已经完整精读和评估过的论文。
- **自动缓存清理**：根据配置的保留天数自动清理 PDF 和 Markdown 文件。

---

## 为什么阅读星星数很重要

在 papers.cool 上，每篇论文都会显示读者点击 **PDF** 和 **Kimi** 的次数。本项目将两者加总为 **reading_stars（阅读星星数）**，作为读者关注度的代理指标。结合类目排名，它能在你花费 LLM token 之前，快速给出哪些论文正在吸引注意的数据化信号。

---

## 安装

推荐 **Python 3.12**。

```bash
# 克隆仓库
git clone git@github.com:AllenX95/daily_coolpapers.git
cd daily_coolpapers

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# 安装依赖
python -m pip install -r requirements.txt
```

为了获得更好的 PDF 转 Markdown 效果，建议额外安装：

```bash
python -m pip install markitdown
```

如果未安装 `markitdown`，系统会自动回退到 `pdfminer`。

---

## 快速启动

### Windows

双击运行：

```text
start_daily_coolpapers.bat
```

脚本会启动本地服务并自动打开浏览器。如果服务已在运行，则只打开网页。

### 手动启动

```bash
python run.py
```

然后打开：

```text
http://127.0.0.1:8765
```

---

## 使用流程

1. **配置 LLM**：进入 **LLM Profiles** 页面，添加 OpenAI-compatible 或 Anthropic 模型配置。API key 会在保存前被加密。
2. **检查 Prompt**：在 **Prompts** 页面查看默认的摘要评估和全文阅读 Prompt，并确保它们绑定到了合适的模型。
3. **设置类目**：启用你关心的 arXiv 类目，并按需调整 `top_n`。
4. **抓取**：在首页点击 **"抓取并摘要评估"**。任务队列会在后台抓取元数据并运行摘要评估。
5. **筛选**：按排名、阅读星星数、评分或关注等级排序；使用日期/类目过滤器缩小范围。
6. **深读**：对感兴趣的论文点击 **"全文阅读"**，下载 PDF 并运行全文 LLM 评估。
7. **导出**：使用 CSV 导出做表格分析，或使用 Markdown 导出做笔记/分享。

---

## 项目结构

```text
daily_coolpapers/
├── app.py              # Flask 应用、路由和 UI 辅助函数
├── crawler.py          # papers.cool HTML 解析和 HTTP 抓取
├── services.py         # 抓取、补抓和 LLM 评估任务的编排
├── llm.py              # OpenAI-compatible 和 Anthropic LLM 客户端
├── prompt_engine.py    # Prompt 渲染和 token 估算
├── default_prompts.py  # 内置摘要/全文 Prompt
├── fulltext.py         # PDF 下载和 PDF 转 Markdown
├── db.py               # SQLite  schema 和数据访问
├── security.py         # API key 加密（DPAPI / Fernet）
├── jobs.py             # 后台任务队列和定时调度
├── cache_manager.py    # PDF/Markdown 缓存和清理
├── config.py           # 路径、默认设置和默认类目
├── static/             # CSS
└── templates/          # Jinja2 HTML 模板

tests/                  # 单元测试
```

---

## 数据与安全

所有数据都保留在本地：

| 路径 | 用途 | 是否提交到 Git |
|---|---|---|
| `data/daily_coolpapers.sqlite3` | 本地 SQLite 数据库 | 否 |
| `instance/` | Flask secret 和加密密钥 | 否 |
| `cache/pdf/` | 下载的 PDF | 否 |
| `cache/markdown/` | 转换后的 Markdown | 否 |
| `logs/current.log` | 运行日志 | 否 |
| `tmp/` | 临时文件 | 否 |
| `.agents/` | 本地 agent 数据 | 否 |

API key 在静态存储时加密；界面中只显示掩码，不会写入日志或 git。

完整排除列表请参见 `.gitignore`。

---

## 配置要点

设置保存在 SQLite 中，可通过 Web UI 编辑：

- `crawler.default_top_n` — 每个类目默认抓取的论文数量。
- `crawler.concurrency` — 抓取时的并行线程数。
- `llm.abstract_concurrency` — 摘要评估时的并行 LLM 调用数。
- `cache.pdf_retention_days` / `cache.markdown_retention_days` — 自动清理保留天数。
- `scheduler.enabled` / `scheduler.daily_times` — 每日自动抓取时间。

---

## 运行测试

```bash
python -B -m unittest discover -s tests
```

---

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 致谢

- 论文元数据来自 [papers.cool](https://papers.cool)，一个对 arXiv 每日更新进行友好展示的镜像站。
- PDF 内容从 arXiv 获取。
