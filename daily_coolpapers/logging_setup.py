import logging
from logging.handlers import RotatingFileHandler

from .config import CURRENT_LOG, ensure_directories


def setup_logging(clear_on_start: bool = True) -> None:
    ensure_directories()
    if clear_on_start:
        CURRENT_LOG.write_text("", encoding="utf-8")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        CURRENT_LOG,
        maxBytes=5 * 1024 * 1024,
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
