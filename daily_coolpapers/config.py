from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / "cache"
PDF_CACHE_DIR = CACHE_DIR / "pdf"
MARKDOWN_CACHE_DIR = CACHE_DIR / "markdown"
LOG_DIR = BASE_DIR / "logs"
CURRENT_LOG = LOG_DIR / "current.log"
DB_PATH = DATA_DIR / "daily_coolpapers.sqlite3"

DEFAULT_SETTINGS = {
    "crawler.default_top_n": 30,
    "crawler.concurrency": 6,
    "crawler.timeout_seconds": 20,
    "crawler.retries": 2,
    "crawler.user_agent": "DailyCoolPapers/0.1",
    "crawler.trust_env_proxy": False,
    "crawler.proxy_url": "",
    "llm.abstract_concurrency": 4,
    "llm.trust_env_proxy": False,
    "llm.pdf_download_timeout_seconds": 300,
    "llm.pdf_download_retries": 2,
    "cache.pdf_retention_days": 5,
    "cache.markdown_retention_days": 7,
    "cache.cleanup_on_start": True,
    "cache.cleanup_daily": True,
    "logs.clear_on_start": True,
    "scheduler.enabled": True,
    "scheduler.daily_times": "10:30,12:00",
}

DEFAULT_CATEGORIES = [
    ("cs.AI", "Artificial Intelligence"),
    ("cs.CL", "Computation and Language"),
    ("cs.CV", "Computer Vision"),
    ("cs.LG", "Machine Learning"),
    ("stat.ML", "Statistics Machine Learning"),
    ("cs.RO", "Robotics"),
    ("cs.IR", "Information Retrieval"),
    ("cs.HC", "Human-Computer Interaction"),
    ("cs.SE", "Software Engineering"),
    ("eess.IV", "Image and Video Processing"),
]


def ensure_directories() -> None:
    for path in [
        INSTANCE_DIR,
        DATA_DIR,
        PDF_CACHE_DIR,
        MARKDOWN_CACHE_DIR,
        LOG_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)
