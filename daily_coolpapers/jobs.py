import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db
from .cache_manager import cleanup_caches
from .services import (SHANGHAI_TZ, build_daily_pipeline_plan, run_daily_pipeline,
                       crawl_all_categories, crawl_to_latest, evaluate_missing_abstracts, evaluate_paper,
                       safe_evaluation_error)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobExecutionResult:
    result: dict[str, Any]
    status: str = "success"

    def __post_init__(self) -> None:
        if self.status not in {"success", "partial_success", "failed"}:
            raise ValueError(f"无效的正常任务终态: {self.status}")


class JobProgressWriter:
    def __init__(
        self,
        job_id: int,
        min_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.job_id = job_id
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self._last_written_key: tuple[int, int, str, str] | None = None
        self._pending: tuple[int, int, str | None, dict[str, Any] | None, tuple[int, int, str, str]] | None = None
        self._last_written_at: float | None = None

    def update(
        self,
        current: int,
        total: int,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        current = max(0, int(current))
        total = max(0, int(total))
        if total and current > total:
            current = total
        details_key = json.dumps(details, ensure_ascii=False, sort_keys=True) if details else ""
        key = (current, total, message or "", details_key)
        if key == self._last_written_key:
            return
        if self._pending and key == self._pending[4]:
            return
        self._pending = (current, total, message, details, key)
        now = self.clock()
        if self._last_written_at is None:
            self.flush(now)
            return
        if total and current >= total:
            self.flush(now)
            return
        if now - self._last_written_at >= self.min_interval_seconds:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        if not self._pending:
            return
        if now is None:
            now = self.clock()
        current, total, message, details, key = self._pending
        db.update_job_progress(self.job_id, current, total, message, details)
        self._last_written_key = key
        self._last_written_at = now
        self._pending = None


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
        with self._state_lock:
            if self._started:
                return
            self._stop_event.clear()
            self._started = True
            self._worker_thread = threading.Thread(target=self._worker_loop, name="job-worker", daemon=True)
            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="job-scheduler", daemon=True)
            self._worker_thread.start()
            self._scheduler_thread.start()
        logger.info("Job runner started")

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._state_lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            threads = [self._worker_thread, self._scheduler_thread]
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(timeout_seconds)
        with self._state_lock:
            self._worker_thread = None
            self._scheduler_thread = None
        logger.info("Job runner stopped")

    def enqueue(self, job_type: str, payload: dict[str, Any] | None = None) -> int:
        with self._state_lock:
            job_id = db.create_job(job_type, payload or {})
            db.update_job_progress(job_id, 0, 1, "等待执行")
            self._queued_job_ids.add(job_id)
            self.queue.put(job_id)
        logger.info("Enqueued job id=%s type=%s", job_id, job_type)
        return job_id

    def active_job_ids(self) -> set[int]:
        with self._state_lock:
            return self._active_job_ids_locked()

    def enqueue_direction_backfill(self,direction_id,date_from,date_to):
        from .services import build_direction_backfill_plan
        plan = build_direction_backfill_plan(direction_id,date_from,date_to)
        with self._state_lock:
            job_id = db.create_direction_backfill_job(plan)
            self._queued_job_ids.add(job_id)
            self.queue.put(job_id)
        return job_id

    def enqueue_memo(self,command):
        from .memos import create_memo_version
        with self._state_lock:
            created = create_memo_version(command)
            if created['created']:
                self._queued_job_ids.add(created['job_id'])
                self.queue.put(created['job_id'])
        return created

    def enqueue_pipeline(
        self, trigger_source: str, category_ids: list[int] | None = None, *,
        start_date: str | None = None, end_date: str | None = None,
        retry_of_job_id: int | None = None, idempotency_key: str | None = None,
        retry_mode: str = 'all',
    ) -> tuple[int, bool]:
        if retry_mode not in {'all', 'abstract_only'}:
            raise ValueError('未知重试模式')
        with self._state_lock:
            active = db.get_active_crawl_job()
            if active:
                return int(active['id']), False
            if idempotency_key:
                with db.connect() as conn:
                    existing = conn.execute('SELECT id FROM jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
                if existing:
                    return int(existing['id']), False
            original_plan = None
            if retry_of_job_id is not None:
                original = db.get_job(retry_of_job_id)
                if not original or original['type'] != db.DAILY_PIPELINE_JOB_TYPE or original['status'] not in db.JOB_TERMINAL_STATUSES:
                    raise ValueError('只能重试已结束的每日情报流水线')
                original_plan = original['payload_data']
                db.pipeline_retry_units(retry_of_job_id)
            plan = build_daily_pipeline_plan(
                trigger_source, category_ids, start_date=start_date, end_date=end_date,
                category_snapshot=original_plan['categories'] if original_plan else None,
            )
            if original_plan:
                plan.update({key: original_plan[key] for key in ('dates', 'start_date', 'end_date', 'target_date', 'categories')})
                original_direction_ids = {item['id'] for item in original_plan.get('directions',[])}
                plan['directions'] = [item for item in plan['directions'] if item['id'] in original_direction_ids]
                plan['retry_mode'] = retry_mode
            job_id, created = db.create_daily_pipeline_job(plan, idempotency_key=idempotency_key, retry_of_job_id=retry_of_job_id)
            if created:
                db.update_job_progress(job_id, 0, 1, '流水线等待执行')
                self._queued_job_ids.add(job_id)
                self.queue.put(job_id)
            return job_id, created

    def _active_job_ids_locked(self) -> set[int]:
        ids = set(self._queued_job_ids)
        if self._running_job_id is not None:
            ids.add(self._running_job_id)
        return ids

    def reconcile_orphaned_pending_jobs(self, min_interval_seconds: float = 0) -> int:
        if not self._started:
            return 0
        with self._state_lock:
            if min_interval_seconds > 0:
                now = time.monotonic()
                if now - self._last_reconcile_at < min_interval_seconds:
                    return 0
                self._last_reconcile_at = now
            marked = db.mark_pending_jobs_interrupted_except(
                self._active_job_ids_locked(),
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
                self.reconcile_orphaned_pending_jobs(min_interval_seconds=30)
                self._maybe_schedule_daily_work()
            except Exception:
                logger.exception("Scheduler loop failed")
            self._stop_event.wait(30)

    def _maybe_schedule_daily_work(self) -> None:
        if not db.get_bool_setting("scheduler.enabled", True):
            return
        now = datetime.now(SHANGHAI_TZ)
        day_key = now.strftime("%Y-%m-%d")
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        self._daily_runs = {key for key in self._daily_runs if key[:10] >= cutoff}
        times = str(db.get_setting("scheduler.daily_times", "10:30,12:00"))
        wanted = {item.strip() for item in times.split(",") if item.strip()}
        current = now.strftime("%H:%M")
        run_key = f"{day_key} {current}"
        if current in wanted and run_key not in self._daily_runs:
            self.enqueue_pipeline("scheduled", idempotency_key=f'scheduled:{run_key}')
            self._daily_runs.add(run_key)
            if db.get_bool_setting("cache.cleanup_daily", True):
                self.enqueue("cleanup", {})

    def _run_job(self, job_id: int) -> None:
        job = db.get_job(job_id)
        if not job or job['status'] != 'pending':
            return
        payload = job.get("payload_data") or {}
        if job['type'] == 'investment_memo_generation':
            # Memo and job transitions must share one SQLite transaction.
            from .memos import generate_memo
            try:
                result = generate_memo(job_id,int(payload['version_id']))
                logger.info('Memo job id=%s finished status=%s',job_id,result['status'])
            except Exception as exc:
                # Persistent DB failure cannot be repaired with a separate job-only
                # write. Leave the nonterminal pair for startup recovery, never call again.
                logger.error('Memo persistence unavailable job=%s error_type=%s',job_id,type(exc).__name__)
            return
        if job['type'] == db.DAILY_PIPELINE_JOB_TYPE:
            db.update_job_with_event(job_id, 'running', event_key=f'pipeline:{job_id}:started',
                                     stage='plan', event_type='pipeline.started')
        else:
            db.update_job(job_id, "running")
        db.update_job_progress(job_id, 0, 1, "任务准备中")
        logger.info("Running job id=%s type=%s", job_id, job["type"])
        try:
            dispatched = self._dispatch(job_id, job["type"], payload)
            if isinstance(dispatched, JobExecutionResult):
                result = dispatched.result
                final_status = dispatched.status
            else:
                result = dispatched
                final_status = "success"
            logger.info("Job id=%s finished result=%s", job_id, _summarize_result(result))
            if job['type'] == db.DAILY_PIPELINE_JOB_TYPE:
                db.update_job_with_event(job_id, final_status, event_key=f'pipeline:{job_id}:completed',
                    stage='finalize', event_type='pipeline.completed', metrics=result,
                    level='info' if final_status == 'success' else 'warning')
            else:
                db.update_job(job_id, final_status)
        except Exception as exc:
            logger.error("Job id=%s failed error_type=%s", job_id, type(exc).__name__)
            if job['type'] == db.DAILY_PIPELINE_JOB_TYPE:
                db.update_job_with_event(job_id, 'failed', event_key=f'pipeline:{job_id}:completed',
                    stage='finalize', event_type='pipeline.completed', level='error',
                    error_code='pipeline_system_error', message='流水线系统错误，请检查配置或数据库',
                    metrics={'status': 'failed', 'error_type': type(exc).__name__})
            else:
                message = safe_evaluation_error(exc) if job['type'] in {'abstract_eval', 'fulltext_eval'} else f"任务失败（{type(exc).__name__}）"
                db.update_job(job_id, "failed", message)

    def _dispatch(
        self,
        job_id: int,
        job_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JobExecutionResult:
        progress_writer = JobProgressWriter(job_id)
        progress_lock = threading.RLock()

        def progress(
            current: int,
            total: int,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            with progress_lock:
                progress_writer.update(current, total, message, details)

        try:
            if job_type == db.DAILY_PIPELINE_JOB_TYPE:
                result = run_daily_pipeline(job_id, payload, progress=progress)
                return JobExecutionResult(result, result['status'])
            if job_type == 'direction_backfill':
                from .services import run_direction_backfill
                result = run_direction_backfill(job_id,payload,progress=progress)
                return JobExecutionResult(result,result['status'])
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
                    result = evaluate_paper(int(paper_id), "abstract_review", payload.get("prompt_id"), job_id=job_id)
                    progress(1, 1, "摘要评估完成")
                    return result
                result = evaluate_missing_abstracts(progress=progress, job_id=job_id)
                status = 'partial_success' if result['failed'] and (result['success'] or result['skipped']) else ('failed' if result['failed'] else 'success')
                return JobExecutionResult(result, status)
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
        finally:
            with progress_lock:
                progress_writer.flush()


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
