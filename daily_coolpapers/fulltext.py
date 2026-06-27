import logging
from pathlib import Path

from pdfminer.high_level import extract_text

from .cache_manager import download_pdf, markdown_path, touch
from .db import get_int_setting

logger = logging.getLogger(__name__)


def ensure_markdown(paper: dict, force: bool = False) -> tuple[Path, bool]:
    arxiv_id = paper["arxiv_id"]
    md_path = markdown_path(arxiv_id)
    if md_path.exists() and not force:
        touch(md_path)
        return md_path, False

    pdf_url = paper.get("pdf_url")
    if not pdf_url:
        raise ValueError("论文没有 PDF URL")

    pdf = download_pdf(
        arxiv_id,
        pdf_url,
        timeout_seconds=get_int_setting("llm.pdf_download_timeout_seconds", 120),
        retries=get_int_setting("llm.pdf_download_retries", 2),
    )
    markdown = convert_pdf_to_markdown(pdf)
    md_path.write_text(markdown, encoding="utf-8")
    return md_path, True


def convert_pdf_to_markdown(pdf_path: Path) -> str:
    try:
        from markitdown import MarkItDown  # type: ignore

        logger.info("Converting PDF with MarkItDown: %s", pdf_path)
        result = MarkItDown().convert(str(pdf_path))
        text = getattr(result, "text_content", None) or str(result)
        return normalize_markdown(text)
    except ImportError:
        logger.warning("MarkItDown 未安装，使用 pdfminer fallback 转换 PDF: %s", pdf_path)
    except Exception:
        logger.exception("MarkItDown 转换失败，尝试 pdfminer fallback: %s", pdf_path)

    text = extract_text(str(pdf_path))
    return normalize_markdown(text)


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("PDF 转换结果为空")
    return text
