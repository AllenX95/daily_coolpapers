import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from . import db
from .cache_manager import cleanup_caches
from .services import crawl_all_categories, crawl_to_latest, evaluate_missing_abstracts, evaluate_paper

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(self) -> None:
        self.queue: queue.Queue[int] = queue.Queue()
        self._started = False
        self._worker_thread: threading.Thread | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._daily_runs: set[str] = set()
        self._state_lock = threading.Lock()
        self._queued_job_ids: set[int] = set()
        self._running_job_id: int | None = None
        self._last_reconcile_at = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker_thread = threading.Thread(target=self._worker_loop, name="job-worker", daemon=True)
        self._worker_thread.start()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="job-scheduler", daemon=True)
        self._scheduler_thread.start()
        logger.info("Job runner started")

    def enqueue(self, job_type: str, payload: dict[str, Any] | None = None) -> int:
        job_id = db.create_job(job_type, payload or {})
        db.update_job_progress(job_id, 0, 1, "等待执行")
        with self._state_lock:
            self._queued_job_ids.add(job_id)
        self.queue.put(job_id)
        logger.info("Enqueued job id=%s type=%s", job_id, job_type)
        return job_id

    def active_job_ids(self) -> set[int]:
        with self._state_lock:
            ids = set(self._queued_job_ids)
            if self._running_job_id is not None:
                ids.add(self._running_job_id)
            return ids

    def reconcile_orphaned_pending_jobs(self, min_interval_seconds: float = 0) -> int:
        if not self._started:
            return 0
        if min_interval_seconds > 0:
            now = time.monotonic()
            with self._state_lock:
                if now - self._last_reconcile_at < min_interval_seconds:
                    return 0
                self._last_reconcile_at = now
        marked = db.mark_pending_jobs_interrupted_except(
            self.active_job_ids(),
            "任务不在当前服务队列中，已标记为中断",
        )
        if marked:
            logger.warning("Marked %s orphaned pending jobs interrupted", marked)
        return marked

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                with self._state_lock:
                    self._queued_job_ids.discard(job_id)
                    self._running_job_id = job_id
                self._run_job(job_id)
            finally:
                with self._state_lock:
                    if self._running_job_id == job_id:
                        self._running_job_id = None
                self.queue.task_done()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._maybe_schedule_daily_work()
            except Exception:
                logger.exception("Scheduler loop failed")
            time.sleep(30)

    def _maybe_schedule_daily_work(self) -> None:
        if not db.get_bool_setting("scheduler.enabled", True):
            return
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        self._daily_runs = {key for key in self._daily_runs if key[:10] >= cutoff}
        times = str(db.get_setting("scheduler.daily_times", "10:30,12:00"))
        wanted = {item.strip() for item in times.split(",") if item.strip()}
        current = now.strftime("%H:%M")
        run_key = f"{day_key} {current}"
        if current in wanted and run_key not in self._daily_runs:
            self._daily_runs.add(run_key)
            self.enqueue("crawl", {})
            if db.get_bool_setting("cache.cleanup_daily", True):
                self.enqueue("cleanup", {})

    def _run_job(self, job_id: int) -> None:
        job = db.get_job(job_id)
        if not job:
            return
        payload = job.get("payload_data") or {}
        db.update_job(job_id, "running")
        db.update_job_progress(job_id, 0, 1, "任务准备中")
        logger.info("Running job id=%s type=%s payload=%s", job_id, job["type"], payload)
        try:
            result = self._dispatch(job_id, job["type"], payload)
            logger.info("Job id=%s finished result=%s", job_id, _summarize_result(result))
            db.update_job(job_id, "success")
        except Exception as exc:
            logger.exception("Job id=%s failed", job_id)
            db.update_job(job_id, "failed", str(exc))

    def _dispatch(self, job_id: int, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        def progress(
            current: int,
            total: int,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            db.update_job_progress(job_id, current, total, message, details)

        if job_type == "crawl":
            return crawl_all_categories(
                payload.get("category_ids"),
                crawl_date=payload.get("crawl_date"),
                progress=progress,
            )
        if job_type == "crawl_catch_up":
            return crawl_to_latest(payload.get("category_ids"), progress=progress)
        if job_type == "abstract_eval":
            paper_id = payload.get("paper_id")
            if paper_id:
                progress(0, 1, "准备摘要评估")
                result = evaluate_paper(int(paper_id), "abstract_review", payload.get("prompt_id"))
                progress(1, 1, "摘要评估完成")
                return result
            return evaluate_missing_abstracts(progress=progress)
        if job_type == "fulltext_eval":
            progress(0, 1, "准备全文阅读")
            result = evaluate_paper(
                int(payload["paper_id"]),
                "fulltext_review",
                payload.get("prompt_id"),
                force_markdown=bool(payload.get("force_markdown")),
            )
            progress(1, 1, "全文阅读完成")
            return result
        if job_type == "cleanup":
            progress(0, 1, "准备清理缓存")
            result = cleanup_caches()
            progress(1, 1, "缓存清理完成")
            return result
        raise ValueError(f"未知任务类型: {job_type}")


job_runner = JobRunner()


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in result.items():
        if key == "result" and isinstance(value, dict):
            summary[key] = {
                "score": value.get("score"),
                "attention": value.get("attention"),
                "tags_count": len(value.get("tags") or []),
            }
        elif key == "categories" and isinstance(value, list):
            summary[key] = {"count": len(value)}
        elif key == "dates" and isinstance(value, list):
            summary[key] = {"count": len(value)}
        else:
            summary[key] = value
    return summary
