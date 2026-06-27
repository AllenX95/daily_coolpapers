# Daily Cool Papers

本项目是一个本地轻量 Web 服务，用于抓取 papers.cool 每日更新的 arXiv 论文，并用 LLM 进行摘要级筛选和按需全文阅读。

## 功能

- 按类目抓取 `https://papers.cool/arxiv/{category}?sort=1` 的 top30 论文。
- 普通 HTTP + HTML 解析，无需浏览器自动化。
- SQLite 本地存储论文、类目排名、Prompt、LLM Profile、评估结果和任务。
- 支持 OpenAI-compatible 与 Anthropic Messages API。
- API key 本地加密保存，UI 只显示掩码。
- 内置摘要评估 Prompt 和全文阅读 Prompt，均包含 VC 投资视角字段。
- Prompt 可编辑、复制、新增，并可为每个 Prompt 绑定不同模型。
- 点击“全文阅读”后才下载 PDF、调用 MarkItDown 转 Markdown 并整篇提交 LLM。
- PDF 默认 5 天清理，Markdown 默认 7 天清理，可在设置页调整。
- 每次服务启动清空 `logs/current.log`，日志页可查看当前运行日志。

## 安装

建议使用 Python 3.12。

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

当前开发环境里 Flask、httpx、BeautifulSoup、cryptography、pdfminer 等依赖已可用；如果没有安装 MarkItDown，全文转换会先提示并 fallback 到 pdfminer。正式使用建议安装 `markitdown`。

## 启动

Windows 下可以直接双击：

```text
start_daily_coolpapers.bat
```

脚本会启动本地服务并自动打开浏览器。如果服务已经在运行，它只会打开网页。

也可以手动启动：

```bash
python run.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## 使用流程

1. 打开 `LLM` 页面，新增 OpenAI-compatible 或 Anthropic Profile。
2. 打开 `Prompt` 页面，确认默认摘要 Prompt 和全文 Prompt 绑定合适模型。
3. 首页点击“抓取并摘要评估”。
4. 需要深读时，在论文行或详情页点击“全文阅读”。
5. 打开 `日志` 页面查看当前运行过程和错误信息。
6. 首页点击“结束服务并退出”可以结束本地服务，并尝试关闭当前网页。

## 数据目录

```text
data/daily_coolpapers.sqlite3
cache/pdf/
cache/markdown/
logs/current.log
instance/
```

`instance/` 中保存本地密钥材料，不要提交到公开仓库。

## 测试

```bash
python -B -m unittest discover -s tests
```
