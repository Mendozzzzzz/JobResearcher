from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from jobresearcher.analysis.audit import audit_pending_jobs
from jobresearcher.analysis.duplicates import review_pending_duplicates
from jobresearcher.analysis.enrich import (
    StaleEnrichmentError,
    enrich_candidate,
    load_pending_enrichment,
)
from jobresearcher.analysis.local import analyze_pending_messages
from jobresearcher.analysis.multi_jobs import review_pending_multi_jobs
from jobresearcher.db import get_conn, init_db
from jobresearcher.fetching.service import fetch_pending_jobs
from jobresearcher.fetching.urls import normalize_pending_urls
from jobresearcher.telegram.collector import TelegramCollector


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineState:
    phase: str = "starting"
    message: str = "Preparazione della pipeline"
    busy: bool = True
    progress: int = 0
    total: int | None = None
    telegram_status: str = "CONNECTING"
    analyzed: int = 0
    fetched: int = 0
    enriched: int = 0
    failed: int = 0
    skipped: int = 0
    stale: int = 0
    urls_normalized: int = 0
    multi_checked: int = 0
    duplicates_checked: int = 0
    audited: int = 0
    last_error: str | None = None
    revision: int = 0


def init_runtime_schema() -> None:
    conn = get_conn()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_phase_backoff (
                phase TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def phase_is_due(phase: str) -> bool:
    conn = get_conn()

    try:
        row = conn.execute(
            """
            SELECT
                CASE
                    WHEN next_attempt_at IS NULL
                      OR datetime(next_attempt_at) <= datetime('now')
                    THEN 1 ELSE 0
                END AS due
            FROM pipeline_phase_backoff
            WHERE phase = ?
            """,
            (phase,),
        ).fetchone()

        return row is None or bool(row["due"])

    finally:
        conn.close()


def record_phase_failure(phase: str, error: str) -> None:
    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT failures
            FROM pipeline_phase_backoff
            WHERE phase = ?
            """,
            (phase,),
        ).fetchone()

        failures = (row["failures"] if row else 0) + 1
        delay = min(300 * (2 ** min(failures - 1, 4)), 3600)

        conn.execute(
            """
            INSERT INTO pipeline_phase_backoff (
                phase, failures, next_attempt_at, last_error
            )
            VALUES (?, ?, datetime('now', ?), ?)
            ON CONFLICT(phase) DO UPDATE SET
                failures = excluded.failures,
                next_attempt_at = excluded.next_attempt_at,
                last_error = excluded.last_error
            """,
            (phase, failures, f"+{delay} seconds", error[:2000]),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def clear_phase_failure(phase: str) -> None:
    conn = get_conn()

    try:
        conn.execute(
            "DELETE FROM pipeline_phase_backoff WHERE phase = ?",
            (phase,),
        )
        conn.commit()
    finally:
        conn.close()


class JobPipeline:
    def __init__(
        self,
        *,
        analysis_batch_size: int = 25,
        fetch_batch_size: int = 5,
        enrichment_batch_size: int = 2,
        idle_interval: float = 10,
        browser_enabled: bool = False,
    ) -> None:
        if min(
            analysis_batch_size,
            fetch_batch_size,
            enrichment_batch_size,
        ) < 1:
            raise ValueError("Batch sizes must be positive")

        if idle_interval <= 0:
            raise ValueError("idle_interval must be positive")

        self.analysis_batch_size = analysis_batch_size
        self.fetch_batch_size = fetch_batch_size
        self.enrichment_batch_size = enrichment_batch_size
        self.idle_interval = idle_interval
        self.browser_enabled = browser_enabled

        self.state = PipelineState()
        self.data_revision = 0

        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._collector_task: asyncio.Task | None = None

    def _update(self, **changes) -> None:
        self.state = replace(
            self.state,
            revision=self.state.revision + 1,
            **changes,
        )

    def _mark_data_changed(self) -> None:
        self.data_revision += 1

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return

        self._stop_event.clear()

        self._task = asyncio.create_task(
            self._run(),
            name="jobresearcher-pipeline",
        )

    async def stop(self) -> None:
        self._stop_event.set()

        self._update(
            phase="stopping",
            message="Stopping; waiting for the current task to finish",
            busy=True,
            total=None,
            progress=0,
        )

        if self._task is not None:
            await self._task
            self._task = None

    async def _collector_loop(self) -> None:
        while not self._stop_event.is_set():
            collector = None
            authentication_required = False

            try:
                collector = TelegramCollector()
                self._update(telegram_status="CONNECTING")

                await asyncio.wait_for(
                    collector.start(),
                    timeout=60,
                )

                self._update(telegram_status="LIVE")

                while (
                    not self._stop_event.is_set()
                    and collector.client.is_connected()
                ):
                    await self._wait(5)

                if not self._stop_event.is_set():
                    self._update(
                        telegram_status="OFFLINE",
                        last_error="Telegram connection lost",
                    )

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

                # Match the collector's current authentication error.
                authentication_required = (
                    "telegram session is not authenticated"
                    in str(exc).casefold()
                )

                logger.exception("Telegram startup failed")

                self._update(
                    telegram_status=(
                        "AUTH REQUIRED"
                        if authentication_required
                        else "OFFLINE"
                    ),
                    last_error=f"Telegram: {error}",
                )

            finally:
                if collector is not None:
                    try:
                        await collector.stop()
                    except Exception:
                        logger.exception("Failed to stop Telegram collector")

            if authentication_required:
                # Do not prompt for login codes in a background retry loop.
                return

            if not self._stop_event.is_set():
                await self._wait(30)

    async def _counted_phase(
        self,
        phase: str,
        title: str,
        worker,
        budget: int,
        success_key: str,
        state_counter: str,
        *,
        threaded: bool,
        single_batch: bool = False,
        worker_kwargs: dict | None = None,
    ) -> dict[str, int]:
        totals = {
            "selected": 0,
            "done": 0,
            "failed": 0,
            "stale": 0,
            "skipped": 0,
        }

        self._update(
            phase=phase,
            message=title,
            busy=True,
            progress=0,
            total=None if single_batch else budget,
        )

        rounds = 1 if single_batch else budget

        for _ in range(rounds):
            if self._stop_event.is_set():
                break

            limit = budget if single_batch else 1
            kwargs = dict(worker_kwargs or {})
            kwargs["limit"] = limit

            if threaded:
                counts = await asyncio.to_thread(worker, **kwargs)
            else:
                counts = await worker(**kwargs)

            selected = counts.get("selected", 0)

            if selected == 0:
                break

            success = counts.get(success_key, 0)
            failed = counts.get("failed", 0)
            stale = counts.get("stale", 0)
            skipped = counts.get("skipped", 0)
                        
                        
            error_detail = counts.get("last_error")


            if isinstance(error_detail, str) and error_detail:
                self._update(last_error=error_detail)

            totals["selected"] += selected
            totals["done"] += success
            totals["failed"] += failed
            totals["stale"] += stale
            totals["skipped"] += skipped

            self._update(
                **{
                    state_counter: getattr(self.state, state_counter)
                    + success
                },
                failed=self.state.failed + failed,
                stale=self.state.stale + stale,
                skipped=self.state.skipped + skipped,
                progress=totals["selected"],
                total=(
                    totals["selected"] if single_batch else budget
                ),
                message=(
                    f"{title} — "
                    f"processed {totals['selected']}, "
                    f"successful {totals['done']}, "
                    f"failed {totals['failed']}"
                )
            )

            if success or skipped:
                self._mark_data_changed()

            if failed or stale:
                break

        self._update(
            total=max(totals["selected"], 1),
            progress=totals["selected"],
        )

        return totals

    async def _analysis_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "analysis",
            "Local analysis",
            analyze_pending_messages,
            self.analysis_batch_size,
            "done",
            "analyzed",
            threaded=True,
        )

    async def _urls_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "urls",
            "URL normalization",
            normalize_pending_urls,
            25,
            "done",
            "urls_normalized",
            threaded=True,
            single_batch=True,
        )

    async def _fetch_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "fetch",
            "Fetching job pages",
            fetch_pending_jobs,
            self.fetch_batch_size,
            "success",
            "fetched",
            threaded=False,
            worker_kwargs={
                "browser_enabled": self.browser_enabled,
                "include_retries": True,
            },
        )

    async def _multi_job_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "multi_jobs",
            "Multi-job review",
            review_pending_multi_jobs,
            25,
            "done",
            "multi_checked",
            threaded=True,
            single_batch=True,
        )

    async def _duplicates_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "duplicates",
            "Duplicate suggestions",
            review_pending_duplicates,
            25,
            "done",
            "duplicates_checked",
            threaded=True,
            single_batch=True,
        )

    async def _audit_phase(self) -> dict[str, int]:
        return await self._counted_phase(
            "audit",
            "Consistency audit",
            audit_pending_jobs,
            25,
            "done",
            "audited",
            threaded=True,
            single_batch=True,
        )

    async def _enrichment_phase(self) -> dict[str, int]:
        ids = await asyncio.to_thread(
            load_pending_enrichment,
            self.enrichment_batch_size,
        )

        counts = {
            "selected": 0,
            "done": 0,
            "failed": 0,
            "stale": 0,
        }

        if not ids:
            return counts

        self._update(
            phase="enrichment",
            message="Qwen / ESCO classification",
            busy=True,
            total=len(ids),
            progress=0,
        )

        for candidate_id in ids:
            if self._stop_event.is_set():
                break

            self._update(
                message=f"Qwen / ESCO — candidate #{candidate_id}",
            )

            try:
                await enrich_candidate(candidate_id)
                counts["done"] += 1

                self._update(
                    enriched=self.state.enriched + 1,
                )
                self._mark_data_changed()

            except StaleEnrichmentError:
                counts["stale"] += 1
                self._update(stale=self.state.stale + 1)

            except Exception as exc:
                counts["failed"] += 1
                logger.exception(
                    "Enrichment failed for candidate #%s",
                    candidate_id,
                )

                self._update(
                    failed=self.state.failed + 1,
                    last_error=(
                        f"Qwen #{candidate_id}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

            counts["selected"] += 1
            self._update(progress=counts["selected"])

        return counts

    async def _run(self) -> None:
        try:
            self._update(
                phase="initializing",
                message="Checking database schema and queues",
                busy=True,
                total=None,
                progress=0,
            )

            await asyncio.to_thread(init_db)
            await asyncio.to_thread(init_runtime_schema)

            if self._stop_event.is_set():
                return

            self._collector_task = asyncio.create_task(
                self._collector_loop(),
                name="jobresearcher-telegram",
            )

            phases = (
                ("analysis", self._analysis_phase),
                ("urls", self._urls_phase),
                ("fetch", self._fetch_phase),
                ("enrichment", self._enrichment_phase),
                ("multi_jobs", self._multi_job_phase),
                ("duplicates", self._duplicates_phase),
                ("audit", self._audit_phase),
            )

            while not self._stop_event.is_set():
                processed = 0

                for phase_name, phase in phases:
                    if self._stop_event.is_set():
                        break

                    if not await asyncio.to_thread(
                        phase_is_due,
                        phase_name,
                    ):
                        continue

                    try:
                        counts = await phase()
                        processed += counts.get("selected", 0)

                        if counts.get("failed", 0):
                            await asyncio.to_thread(
                                record_phase_failure,
                                phase_name,
                                (
                                    f"{counts['failed']} batch errors; "
                                    "phase temporarily paused"
                                ),
                            )

                            detail = self.state.last_error

                            has_specific_detail = (
                                isinstance(detail, str)
                                and (
                                    (
                                        phase_name == "fetch"
                                        and detail.startswith("fetch #")
                                    )
                                    or (
                                        phase_name == "enrichment"
                                        and detail.startswith("Qwen #")
                                    )
                                )
                            )

                            self._update(
                                last_error=(
                                    f"{detail} — retry deferred"
                                    if has_specific_detail
                                    else (
                                        f"{phase_name}: batch errors; "
                                        "retry deferred. Check the logs."
                                    )
                                ),
                            )

                        else:
                            await asyncio.to_thread(
                                clear_phase_failure,
                                phase_name,
                            )

                    except Exception as exc:
                        logger.exception(
                            "Pipeline phase %s failed",
                            phase_name,
                        )

                        error = f"{type(exc).__name__}: {exc}"

                        self._update(
                            failed=self.state.failed + 1,
                            last_error=f"{phase_name}: {error}",
                        )

                        await asyncio.to_thread(
                            record_phase_failure,
                            phase_name,
                            error,
                        )

                if self._stop_event.is_set():
                    break

                self._update(
                    phase="idle",
                    message=(
                        "Waiting for the next check; retries or pending reviews may remain"
                    ),
                    busy=False,
                    total=1,
                    progress=1,
                )

                # Pause between checks to avoid tight loops when reviews keep changing.
                await self._wait(
                    1 if processed else self.idle_interval
                )

        except Exception as exc:
            logger.exception("Pipeline startup failed")

            self._update(
                phase="error",
                message="Pipeline failed to start",
                busy=False,
                total=1,
                progress=0,
                last_error=f"{type(exc).__name__}: {exc}",
                failed=self.state.failed + 1,
            )

        finally:
            if self._collector_task is not None:
                self._collector_task.cancel()

                await asyncio.gather(
                    self._collector_task,
                    return_exceptions=True,
                )

                self._collector_task = None

            if self.state.phase != "error":
                self._update(
                    phase="stopped",
                    message="Pipeline arrestata",
                    busy=False,
                    telegram_status="STOPPED",
                    total=1,
                    progress=1,
                )