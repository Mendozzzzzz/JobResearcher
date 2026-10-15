from __future__ import annotations

import shlex
import asyncio
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

from django.db.migrations import state
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Static,
    Button,
    ProgressBar,
)
from jobresearcher.analysis.enrich import enrich_candidate
from jobresearcher.db import (
    get_conn,
    get_inbox_jobs,
    get_job_detail,
    get_message_by_id,
    get_recent_messages,
    get_stats,
    init_db,
    set_user_status,
    set_multi_job
)
from jobresearcher.pipeline import JobPipeline


USER_STATUS_LABELS = {
    "new": "NEW",
    "interested": "INTERESTED",
    "rejected": "REJECTED",
    "applied": "APPLIED",
    "archived": "ARCHIVED",
}

ANALYSIS_STYLES = {
    "enriched": "green",
    "needs_review": "yellow",
    "rejected_non_job": "red",
    "pending": "dim",
    "rules_complete": "cyan",
}

USER_STATUS_STYLES = {
    "new": "white",
    "interested": "cyan",
    "rejected": "red",
    "applied": "green",
    "archived": "dim",
}
SORT_OPTIONS = (
    ("auto", "Default"),
    ("relevance", "Search relevance"),
    ("recent", "Newest"),
    ("oldest", "Oldest"),
    ("company", "Company"),
    ("title", "Title"),
    ("quality", "Extraction quality"),
    ("interested", "Interested first"),
)
def format_salary(row) -> str:
    minimum = row["salary_min"]
    maximum = row["salary_max"]
    currency = row["salary_currency"] or ""
    period = row["salary_period"] or ""

    if minimum is None and maximum is None:
        return "---"

    if minimum is not None and maximum is not None:
        amount = f"{int(minimum):,}-{int(maximum):,}".replace(",", ".")
    elif minimum is not None:
        amount = f"{int(minimum):,}+".replace(",", ".")
    else:
        amount = f"<= {int(maximum):,}".replace(",", ".")

    result = amount

    if currency:
        result += f" {currency}"

    if period and period != "unknown":
        result += f"/{period}"

    return result


def format_location(row) -> str:
    location_type = row["location_type"] or "unknown"
    location_text = row["location_text"] or ""

    if location_text:
        return f"{location_type} · {location_text}"

    return location_type


def format_skills(raw_skills) -> str:
    if not raw_skills:
        return "—"

    if isinstance(raw_skills, str):
        try:
            import json

            parsed = json.loads(raw_skills)
            if isinstance(parsed, list):
                raw_skills = parsed
        except (json.JSONDecodeError, TypeError):
            raw_skills = [raw_skills]

    if isinstance(raw_skills, list):
        return ", ".join(str(item) for item in raw_skills[:20]) or "—"

    return str(raw_skills)




class JobResearcherApp(App):
    TITLE = "JobResearcher"

    CSS = """
    Screen {
        background: #090b0f;
        color: #d6dce7;
    }

    #app-header {
        height: 1;
        padding: 0 2;
        background: #090b0f;
        color: #e7edf8;
        text-style: bold;
    }

    #status-line {
        height: 1;
        padding: 0 2;
        color: #78d8c8;
        background: #090b0f;
    }

    #rule {
        height: 1;
        color: #2b3c44;
        padding: 0 1;
    }

    #search {
        height: 3;
        margin: 0 2 1 2;
        border: none;
        background: #11161b;
        color: #e6fbf6;
    }

    #search:focus {
        border: tall #65cbb8;
    }

    #results-label {
        height: 1;
        padding: 0 2;
        color: #81949b;
        text-style: bold;
    }

    DataTable {
        height: 1fr;
        min-height: 5;
        margin: 0 2;
        background: #090b0f;
        color: #d6dce7;
        border: none;
    }

    DataTable > .datatable--header {
        background: #11161b;
        color: #8edbcc;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #182722;
        color: #f1fff9;
    }

    #detail-scroll {
        height: 1fr;
        min-height: 4;
        margin: 1 2 0 2;
        background: #0d1216;
        border-top: solid #34564e;
    }

    #detail {
        height: auto;
        padding: 1 2;
        background: #0d1216;
        color: #cfd8dd;
    }

    .hidden {
        display: none;
    }
    Footer {
        background: #090b0f;
        color: #77858d;
    }

    Footer > .footer--key {
        color: #70cfbe;
        text-style: bold;
    }
    #pipeline-status {
        height: 2;
        margin: 0 2;
        color: #9acdc4;
    }

    #pipeline-progress {
        height: 1;
        margin: 0 2 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh_data", "reload"),
        Binding("j", "show_jobs", "jobs"),
        Binding("m", "show_messages", "messages"),
        Binding("u", "cycle_user_filter", "user"),
        Binding("v", "toggle_multi_filter", "show/hide multi"),
        Binding("g", "toggle_selected_multi_job", "mark multi"),
        Binding("e", "enrich_selected", "enrich"),
        Binding("i", "mark_interested", "interested"),
        Binding("x", "mark_rejected", "reject"),
        Binding("a", "mark_applied", "applied"),
        Binding("z", "mark_archived", "archive"),
        Binding("n", "mark_new", "reset"),
        Binding("o", "open_selected_url", "open url"),
        Binding("c", "toggle_collector", "telegram status"),
        Binding("/", "focus_search", "search"),
        Binding(
            "escape",
            "close_detail_or_clear_search",
            "back",
            priority=True,
        ),
        Binding("s", "cycle_sort", "sort"),
        Binding("d", "toggle_duplicates","duplicates"),
        Binding("ctrl+r","reset_inbox_filters", "reset filters"),
    ]

    pipeline: JobPipeline | None = None
    collector_status = "CONNECTING"

    _pipeline_ui_revision = -1
    _pipeline_data_revision = -1
    _pipeline_closing = False

    mode = "jobs"
    user_filter = "active"
    include_multi_job = False
    include_duplicates=False
    sort_by= "auto"
    pending_url: str | None = None
    enriching_ids: set[int] = set()
    detail_open = False
    detail_candidate_id: int | None = None


    def restore_list_view(self) -> None:
        self.detail_open = False
        self.detail_candidate_id = None
        self.pending_url = None

        self.query_one("#search", Input).remove_class("hidden")
        self.query_one("#results-label", Static).remove_class("hidden")
        self.query_one("#jobs-table", DataTable).remove_class("hidden")

        self.query_one("#detail", Static).remove_class("fullscreen")

    def compose(self) -> ComposeResult:
        yield Static("JOBRESEARCHER", id="app-header")
        yield Static("loading local index...", id="status-line")
        yield Static(
            "Starting the processing pipeline...",
            id="pipeline-status",
        )

        yield ProgressBar(
            total=None,
            show_eta=False,
            show_percentage=False,
            id="pipeline-progress",
        )
        yield Static("─" * 120, id="rule")

        yield Input(
            placeholder="Search: rust, zk, solidity, remote, security...",
            id="search",
        )

        yield Static("INBOX", id="results-label")
        yield DataTable(id="jobs-table")

        with VerticalScroll(id="detail-scroll"):
            yield Static(
                Text(
                    "Select a job and press Enter to view its salary, "
                    "extracted fields, and post text."
                ),
                id="detail",
            )

        yield Footer()

    async def on_mount(self) -> None:
        await asyncio.to_thread(init_db)

        self._pipeline_ui_revision = -1
        self._pipeline_data_revision = -1
        self._pipeline_closing = False

        self.show_jobs()
        self.update_status()

        self.pipeline = JobPipeline(
            analysis_batch_size=25,
            fetch_batch_size=5,
            enrichment_batch_size=2,
            idle_interval=10,
            browser_enabled=False,
        )

        self.pipeline.start()

        self.set_interval(0.5, self.refresh_pipeline_ui)
        self.set_interval(15, self.refresh_status_only)


    def refresh_pipeline_ui(self) -> None:
        if self.pipeline is None or self._pipeline_closing:
            return

        state = self.pipeline.state

        if state.revision != self._pipeline_ui_revision:
            self._pipeline_ui_revision = state.revision
            self.collector_status = state.telegram_status

            text = Text()
            text.append(state.message + "\n")

            text.append(
                f"Analyzed: {state.analyzed} · "
                f"URL: {state.urls_normalized} · "
                f"Pages: {state.fetched} · "
                f"Enriched: {state.enriched} · "
                f"Multi: {state.multi_checked} · "
                f"Dedup: {state.duplicates_checked} · "
                f"Audit: {state.audited} · "
                f"Errors: {state.failed} · "
                f"Skipped: {state.skipped}",
                style="dim",
            )
            if state.last_error:
                text.append(
                    f" · Last error: {state.last_error}",
                    style="yellow",
                )

            self.query_one(
                "#pipeline-status",
                Static,
            ).update(text)

            self.query_one(
                "#pipeline-progress",
                ProgressBar,
            ).update(
                total=state.total,
                progress=state.progress,
            )

            self.update_status()

        if (
            self.pipeline.data_revision
            == self._pipeline_data_revision
        ):
            return

        if self.detail_open or self.pending_url is not None:
            return

        if self.query_one("#search", Input).has_focus:
            return

        query = self.query_one("#search", Input).value

        if self.mode == "jobs":
            self.refresh_jobs_preserving_selection(query)
        else:
            self.refresh_messages_preserving_selection(query)

        self._pipeline_data_revision = self.pipeline.data_revision

    def refresh_jobs_preserving_selection(
        self,
        query: str,
    ) -> None:
        selected_id = self.get_selected_candidate_id()

        self.refresh_jobs(query)

        if selected_id is None:
            return

        table = self.query_one("#jobs-table", DataTable)

        for row_index in range(table.row_count):
            values = table.get_row_at(row_index)

            if values and str(values[0]) == str(selected_id):
                table.move_cursor(
                    row=row_index,
                    animate=False,
                )
                break

    def refresh_messages_preserving_selection(
        self,
        query: str,
    ) -> None:
        table = self.query_one("#jobs-table", DataTable)
        selected_key = None

        if 0 <= table.cursor_row < table.row_count:
            selected_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key

        self.refresh_messages(query)

        if selected_key is None:
            return

        try:
            row_index = table.get_row_index(selected_key)
        except KeyError:
            return

        table.move_cursor(
            row=row_index,
            animate=False,
        )

    def refresh_status_only(self) -> None:
        self.update_status()

    def update_status(self) -> None:
        stats = get_stats()

        status = (
            f"{stats['total_messages']} POSTS · "
            f"{stats['total_channels']} SOURCES · "
            f"TELEGRAM: {self.collector_status} · "
            f"USER: {self.user_filter.upper()} · "
            f"MULTI: {'SHOW' if self.include_multi_job else 'HIDE'} · "
            f"MODE: {self.mode.upper()}"
        )

        if self.mode == "jobs":
            status += (
                f" · SORT: {self.sort_by.upper()} · "
                f"DUP: {'SHOW' if self.include_duplicates else 'HIDE'}"
            )

        self.query_one("#status-line", Static).update(status)

    def refresh_all(self) -> None:
        self.update_status()
        query = self.query_one("#search", Input).value

        if self.mode == "jobs":
            self.refresh_jobs(query)
        else:
            self.refresh_messages(query)

    def clear_table(self) -> DataTable:
        table = self.query_one("#jobs-table", DataTable)
        table.clear(columns=True)
        return table

    def show_jobs(self) -> None:
        self.restore_list_view()
        self.mode = "jobs"

        table = self.clear_table()
        table.cursor_type = "row"

        columns = [
            ("ID", 5),
            ("DATE LOCAL", 16),
            ("MATCH", 6),
            ("TITLE", 28),
            ("COMPANY", 18),
            ("SALARY", 25),
            ("LEVEL", 9),
            ("LOCATION", 16),
            ("ANALYSIS", 12),
            ("USER", 10),
        ]

        for label, width in columns:
            table.add_column(label, width=width)

        self.query_one("#results-label", Static).update("INBOX")
        self.refresh_jobs(self.query_one("#search", Input).value)

        table.focus()

    def show_messages(self) -> None:
        self.restore_list_view()
        self.mode = "messages"

        table = self.clear_table()
        table.cursor_type = "row"

        table.add_columns(
            "SOURCE",
            "TIMESTAMP",
            "MESSAGE",
        )

        self.query_one("#results-label", Static).update("MESSAGES")
        self.refresh_messages(self.query_one("#search", Input).value)
        table.focus()

    @staticmethod
    def parse_inbox_search(value: str) -> tuple[str, dict]:
        filters = {}
        terms = []

        field_names = {
            "location": "location_query",
            "work": "location_type",
            "seniority": "seniority",
            "contract": "employment_type",
            "degree": "degree_status",
            "salary": "salary_known",
            "page": "page_available",
        }

        allowed_values = {
            "location_type": {
                "remote", "hybrid", "onsite", "unknown"
            },
            "seniority": {
                "junior", "mid", "senior", "lead",
                "principal", "unknown"
            },
            "employment_type": {
                "full_time", "part_time", "contract",
                "internship", "unknown"
            },
            "degree_status": {
                "required", "preferred",
                "not_required", "unknown"
            },
        }

        def parse_bool(text: str) -> bool:
            normalized = text.casefold()

            if normalized in {"true", "yes", "1"}:
                return True

            if normalized in {"false", "no", "0"}:
                return False

            raise ValueError(
                f"Invalid boolean value: {text!r}. "
                "Use true or false."
            )

        for token in shlex.split(value):
            key, separator, raw = token.partition(":")
            key = key.casefold()

            if not separator or key not in {
                *field_names,
                "remote",
            }:
                terms.append(token)
                continue

            if not raw:
                raise ValueError(
                    f"Missing value for filter {key}:"
                )

            if key == "remote":
                if not parse_bool(raw):
                    raise ValueError(
                        "Use work:onsite or work:hybrid "
                        "instead of remote:false."
                    )

                target = "location_type"
                parsed = "remote"

            else:
                target = field_names[key]

                if target in {"salary_known", "page_available"}:
                    parsed = parse_bool(raw)
                elif target == "location_query":
                    parsed = raw
                else:
                    parsed = raw.casefold()

                    if parsed not in allowed_values[target]:
                        choices = ", ".join(
                            sorted(allowed_values[target])
                        )
                        raise ValueError(
                            f"Invalid value for {key}: {raw!r}. "
                            f"Allowed values: {choices}"
                        )

            if target in filters:
                raise ValueError(
                    f"Filter specified more than once: {key}. "
                    "Use only one value."
                )

            filters[target] = parsed

        return " ".join(terms), filters

    def refresh_jobs(self, query: str = "") -> None:
        table = self.query_one("#jobs-table", DataTable)
        self.pending_url = None

        try:
            text_query, filters = self.parse_inbox_search(query)

            rows = get_inbox_jobs(
                query=text_query,
                user_status=self.user_filter,
                limit=300,
                include_multi_job=self.include_multi_job,
                include_duplicates=self.include_duplicates,
                sort_by=self.sort_by,
                **filters,
            )

        except Exception as exc:
            table.clear()
            self.query_one("#results-label", Static).update(
                Text(f"Inbox error: {exc}", style="yellow")
            )
            return

        table.clear()

        for row in rows:

            match_text = (
                f"{row['relevance_score']:.0f}"
                if text_query.strip()
                else "—"
            )
            title_text = Text()

            if row["duplicate_of"] is not None:
                title_text.append("[DUP] ", style="bold yellow")

            if row["is_multi_job"]:
                title_text.append("[MULTI] ", style="bold magenta")

            title_text.append(row["title"] or "(no title)")

            analysis = row["analysis_status"] or "unknown"
            user_status = row["user_status"] or "new"

            table.add_row(           
                str(row["id"]),
                self.format_date(row["posted_at"]),
                match_text,
                title_text,
                Text(row["company"] or "?"),
                Text(format_salary(row)),
                Text(row["seniority"] or "?"),
                Text(format_location(row)),
                Text(
                    analysis,
                    style=ANALYSIS_STYLES.get(analysis, "white"),
                ),
                Text(
                    USER_STATUS_LABELS.get(
                        user_status,
                        user_status.upper(),
                    ),
                    style=USER_STATUS_STYLES.get(user_status, "white"),
                ),
                key=str(row["id"]),
            )

        sort_label = dict(SORT_OPTIONS)[self.sort_by]
        label = (
            f"INBOX {len(rows)}"
            f" · SORT: {sort_label}"
            f" · USER: {self.user_filter.upper()}"
            f" · MULTI: {'SHOW' if self.include_multi_job else 'HIDE'}"
            f" · DUP: {'SHOW' if self.include_duplicates else 'HIDE'}"
        )

        if query.strip():
            label += f" · QUERY: {query.strip()}"

        self.query_one("#results-label", Static).update(Text(label))
        table.refresh(layout=True)
    def refresh_messages(self, query: str = "") -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.clear()

        rows = get_recent_messages(query=query, limit=100)

        for row in rows:
            date_text = self.format_date(row["date"])
            preview = " ".join((row["text"] or "").split())

            if len(preview) > 92:
                preview = f"{preview[:89]}..."

            table.add_row(
                f"@{row['channel']}",
                date_text,
                preview or "[no text]",
                key=str(row["id"]),
            )

        label = f"MESSAGES {len(rows)}"

        if query.strip():
            label += f" · QUERY: {query.strip()}"

        self.query_one("#results-label", Static).update(label)
        table.refresh(layout=True)

    @staticmethod
    def format_date(value) -> str:
        if not value:
            return "—"

        raw = str(value)

        try:
            parsed = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )

            if parsed.tzinfo is not None:
                parsed = parsed.astimezone()

            return parsed.strftime("%Y-%m-%d %H:%M")

        except ValueError:
            return raw[:16]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.refresh_all()

    def on_data_table_row_selected(
        self,
        event: DataTable.RowSelected,
    ) -> None:
        if event.row_key is None:
            return

        record_id = int(event.row_key.value)

        if self.mode == "jobs":
            self.show_job_detail_fullscreen(record_id)
            return

       
        conn = get_conn()
        try:
            candidate = conn.execute(
                """
                SELECT id
                FROM job_candidates
                WHERE message_db_id = ?
                """,
                (record_id,),
            ).fetchone()
        finally:
            conn.close()

        if candidate is not None:
            self.show_job_detail_fullscreen(candidate["id"])
        else:
            self.show_message_detail(record_id)
            self.notify(
                "No job candidate is linked; showing the Telegram post only.",
                timeout=3.0,
            )
    def show_job_detail(self, candidate_id: int) -> None:
        row = get_job_detail(candidate_id)

        if row is None:
            return

        page_text = row["page_text"] or ""
        page_preview = page_text[:5000]

        if len(page_text) > 5000:
            page_preview += "\n\n[...truncated...]"

        telegram_text = row["message_text"] or row["raw_telegram_text"] or ""
        telegram_preview = telegram_text[:3000]

        if len(telegram_text) > 3000:
            telegram_preview += "\n\n[...truncated...]"

        analysis = row["analysis_status"] or "unknown"
        user_status = row["user_status"] or "new"

        detail = Text()
        detail.append(f"JOB #{row['id']}\n", style="bold #7dd8c7")
        detail.append(
            f"{row['title'] or '(no title)'} · {row['company'] or '?'}\n",
            style="bold",
        )

        detail.append(
            f"analysis={analysis} · "
            f"user={USER_STATUS_LABELS.get(user_status, user_status)}\n",
            style="dim",
        )

        detail.append(
            f"level={row['seniority'] or '?'} · "
            f"type={row['employment_type'] or '?'} · "
            f"location={format_location(row)}\n",
            style="dim",
        )

        detail.append(
            f"salary={format_salary(row)} · "
            f"degree={row['degree_status'] or '?'}\n",
            style="dim",
        )

        detail.append(
            f"skills={format_skills(row['skills'])}\n",
            style="dim",
        )

        detail.append(
            f"fetch={row['fetch_status'] or '?'} · "
            f"external_job={row['external_job_status'] or '?'} · "
            f"multi={'YES' if row['is_multi_job'] else 'no'}\n",
            style="dim",
        )

        detail.append(
            f"external: {row['external_url'] or '-'}\n",
            style="dim",
        )

        detail.append(
            f"apply: {row['application_url'] or '-'}\n\n",
            style="dim",
        )

        detail.append("TELEGRAM\n", style="bold #8edbcc")
        detail.append(f"{telegram_preview or '[no text]'}\n\n")
        detail.append("PAGE TEXT\n", style="bold #8edbcc")
        detail.append(page_preview or "[not fetched]")

        self.query_one("#detail", Static).update(detail)
        self.query_one(
            "#detail-scroll",
            VerticalScroll,
        ).scroll_home(animate=False)

    def show_message_detail(self, message_id: int) -> None:
        row = get_message_by_id(message_id)

        if row is None:
            return

        channel = escape(str(row["channel"]))
        date = escape(str(row["date"]))
        text = escape(row["text"] or "[no text]")

        detail = (
            f"[bold #7dd8c7]@{channel}[/bold #7dd8c7] "
            f"[dim]{date} · msg #{row['message_id']}[/dim]\n\n"
            f"{text}"
        )

        self.query_one("#detail", Static).update(detail)

    def get_selected_candidate_id(self) -> int | None:
        if self.mode != "jobs":
            return None

        if self.detail_open:
            return self.detail_candidate_id

        table = self.query_one("#jobs-table", DataTable)

        if table.row_count == 0:
            return None

        cursor_row = table.cursor_row

        if cursor_row < 0 or cursor_row >= table.row_count:
            return None

        row_values = table.get_row_at(cursor_row)

        if not row_values:
            return None

        try:
            return int(row_values[0])
        except (TypeError, ValueError):
            return None

        
    def action_open_detail(self) -> None:
        if self.mode != "jobs":
            return

        candidate_id = self.get_selected_candidate_id()

        if candidate_id is None:
            self.notify("Select a job first", severity="warning")
            return

        self.show_job_detail_fullscreen(candidate_id)

    def show_job_detail_fullscreen(self, candidate_id: int) -> None:
        row = get_job_detail(candidate_id)

        if row is None:
            self.notify("Job not found", severity="error")
            return

        page_text = (row["page_text"] or "").strip()
        telegram_text = (
            row["message_text"]
            or row["raw_telegram_text"]
            or ""
        ).strip()

        detail = Text()

        detail.append(
            f"JOB #{row['id']} — "
            f"{row['title'] or '(no title)'}\n",
            style="bold #7dd8c7",
        )
        detail.append(
            f"{row['company'] or '?'} · "
            f"status={row['analysis_status'] or '?'}\n\n",
            style="dim",
        )

        detail.append(
            f"SALARY: {format_salary(row)}\n"
            f"LEVEL: {row['seniority'] or 'unknown'} · "
            f"TYPE: {row['employment_type'] or 'unknown'}\n"
            f"LOCATION: {format_location(row)}\n"
            f"MULTI: {'YES' if row['is_multi_job'] else 'no'}\n"
            f"SKILLS: {format_skills(row['skills'])}\n\n",
            style="bold",
        )

        detail.append(
            f"DATA TELEGRAM: {self.format_date(row['posted_at'])}\n",
            style="dim",
        )

        if row["duplicate_of"] is not None:
            detail.append(
                f"DUPLICATO DI: #{row['duplicate_of']}\n",
                style="bold yellow",
            )

        detail.append("\n")

        detail.append(
            "DESCRIPTION / PAGE TEXT\n\n",
            style="bold #8edbcc",
        )

        if page_text:
            detail.append(page_text)
        else:
            detail.append(
                "[No page text was fetched for this job.]\n",
                style="yellow",
            )

        detail.append(
            "\n\nTELEGRAM MESSAGE\n\n",
            style="bold #8edbcc",
        )
        detail.append(
            telegram_text or "[No Telegram message available.]"
        )

        detail.append(
            "\n\nLINKS\n\n",
            style="bold #8edbcc",
        )
        detail.append(
            f"External: {row['external_url'] or '—'}\n"
            f"Apply: {row['application_url'] or '—'}\n\n"
            "Press Esc to return to the Inbox."
        )

        self.detail_open = True
        self.detail_candidate_id = candidate_id
        self.pending_url = None

        self.query_one("#search", Input).add_class("hidden")
        self.query_one("#results-label", Static).add_class("hidden")
        self.query_one("#jobs-table", DataTable).add_class("hidden")

        self.query_one("#detail", Static).update(detail)

        reader = self.query_one("#detail-scroll", VerticalScroll)
        reader.focus()
        self.call_after_refresh(
            reader.scroll_home,
            animate=False,
        )

    def action_close_detail_or_clear_search(self) -> None:
        self.pending_url = None

        if self.detail_open:
            self.restore_list_view()

            self.query_one("#detail", Static).update(
                Text(
                    "Select an item and press Enter to view its contents."
                )
            )

            self.query_one("#jobs-table", DataTable).focus()
            return

        self.query_one("#jobs-table", DataTable).focus()

    def action_refresh_data(self) -> None:
        self.refresh_all()
        self.notify("Index reloaded", timeout=1.5)

    def action_show_jobs(self) -> None:
        self.show_jobs()
        self.notify("Jobs inbox", timeout=1.0)

    def action_show_messages(self) -> None:
        self.show_messages()
        self.notify("Messages view", timeout=1.0)

    def action_cycle_user_filter(self) -> None:
        if self.mode != "jobs":
            self.notify("User filter is available in Jobs view", severity="warning")
            return

        order = [
            "active",
            "new",
            "interested",
            "rejected",
            "applied",
            "archived",
            "all",
        ]

        index = order.index(self.user_filter)
        self.user_filter = order[(index + 1) % len(order)]

        self.refresh_jobs(self.query_one("#search", Input).value)
        self.notify(f"User filter: {self.user_filter}", timeout=1.5)


    def action_toggle_multi_filter(self) -> None:
        if self.mode != "jobs":
            self.notify(
                "The multi-job filter is available in Jobs view.",
                severity="warning",
            )
            return

        if self.detail_open:
            self.restore_list_view()

            self.query_one("#detail", Static).update(
                Text("Select a job and press Enter to view it.")
            )

        self.pending_url = None
        self.include_multi_job = not self.include_multi_job

        self.refresh_jobs(
            self.query_one("#search", Input).value
        )
        self.update_status()
        self.query_one("#jobs-table", DataTable).focus()

        self.notify(
            "Multi-job candidates are included in the list."
            if self.include_multi_job
            else "Multi-job candidates are hidden from the list.",
            timeout=2.0,
        )

    def action_toggle_selected_multi_job(self) -> None:
        if self.mode != "jobs":
            self.notify(
                "Switch to Jobs view with j to mark a candidate as multi-job.",
                severity="warning",
            )
            return

        candidate_id = self.get_selected_candidate_id()

        if candidate_id is None:
            self.notify(
                "Select a job before pressing g.",
                severity="warning",
            )
            return

        row = get_job_detail(candidate_id)

        if row is None:
            self.notify("Job not found.", severity="error")
            return

        new_value = not bool(row["is_multi_job"])
        was_detail_open = self.detail_open
        self.pending_url = None

        try:
            set_multi_job(candidate_id, new_value)
        except Exception as exc:
            self.notify(
                f"Could not update multi-job flag: {exc}",
                severity="error",
            )
            return

        # Also refresh the list behind the detail view.
        self.refresh_jobs(
            self.query_one("#search", Input).value
        )

        if was_detail_open:
            self.show_job_detail_fullscreen(candidate_id)
        else:
            self.show_job_detail(candidate_id)
            self.query_one("#jobs-table", DataTable).focus()

        self.update_status()

        if new_value:
            message = f"Job #{candidate_id}: marked as multi-job."

            if not self.include_multi_job:
                message += (
                    " Hidden from the list; press v to show it."
                )
        else:
            message = f"Job #{candidate_id}: multi-job flag removed."

        self.notify(message, timeout=3.0)


    def prepare_inbox_controls(self) -> bool:
        if self.mode != "jobs":
            self.notify(
                "Filters and sorting are available in Jobs view.",
                severity="warning",
            )
            return False

        if self.detail_open:
            self.restore_list_view()

            self.query_one("#detail", Static).update(
                Text(
                    "Select a job and press Enter to view it."
                )
            )

        self.pending_url = None
        return True

    def action_cycle_sort(self) -> None:
        if not self.prepare_inbox_controls():
            return

        keys = [key for key, _label in SORT_OPTIONS]
        index = keys.index(self.sort_by)
        self.sort_by = keys[(index + 1) % len(keys)]

        self.refresh_jobs(
            self.query_one("#search", Input).value
        )
        self.update_status()
        self.query_one("#jobs-table", DataTable).focus()

        self.notify(
            f"Sort order: {dict(SORT_OPTIONS)[self.sort_by]}",
            timeout=2.0,
        )

    def action_toggle_duplicates(self) -> None:
        if not self.prepare_inbox_controls():
            return

        self.include_duplicates = not self.include_duplicates

        self.refresh_jobs(
            self.query_one("#search", Input).value
        )
        self.update_status()
        self.query_one("#jobs-table", DataTable).focus()

        self.notify(
            "Candidates marked as duplicates are included."
            if self.include_duplicates
            else "Candidates marked as duplicates are hidden.",
            timeout=2.0,
        )

    def action_reset_inbox_filters(self) -> None:
        if not self.prepare_inbox_controls():
            return

        self.user_filter = "active"
        self.include_multi_job = False
        self.include_duplicates = False
        self.sort_by = "auto"

        self.query_one("#search", Input).value = ""

        self.refresh_jobs("")
        self.update_status()
        self.query_one("#jobs-table", DataTable).focus()

        self.notify(
            "Filters cleared; default sorting restored.",
            timeout=2.0,
        )

    def action_focus_search(self) -> None:
        if self.detail_open:
            self.restore_list_view()

        self.pending_url = None
        self.query_one("#search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#jobs-table", DataTable).focus()

    

    def action_clear_search(self) -> None:
        self.query_one("#search", Input).value = ""
        self.query_one("#jobs-table", DataTable).focus()

    def action_enrich_selected(self) -> None:
        self.notify(
            "Pending posts are processed automatically. "
            "Manual reprocessing will use the same queue.",
            timeout=4.0,
        )
        candidate_id = self.get_selected_candidate_id()

        if candidate_id is None:
            self.notify("No job selected", severity="warning")
            return

        if candidate_id in self.enriching_ids:
            self.notify(
                f"Job #{candidate_id} is already enriching",
                severity="warning",
            )
            return

        self.enriching_ids.add(candidate_id)
        self.notify(f"Enriching job #{candidate_id}...", timeout=2.0)

        asyncio.create_task(self.run_enrichment(candidate_id))

    async def run_enrichment(self, candidate_id: int) -> None:
        try:
            result = await enrich_candidate(candidate_id)

            if self.mode == "jobs":
                self.refresh_jobs(
                    self.query_one("#search", Input).value
                )

                if self.detail_open:
                    if self.detail_candidate_id == candidate_id:
                        self.show_job_detail_fullscreen(candidate_id)
                else:
                    self.show_job_detail(candidate_id)

            self.notify(
                f"Job #{candidate_id}: "
                f"{result.get('content_type', '?')} / "
                f"{result.get('analysis_status', '?')}",
                timeout=3.0,
            )

        except Exception as exc:
            self.notify(
                f"Enrichment failed: {exc}",
                severity="error",
            )

        finally:
            self.enriching_ids.discard(candidate_id)

    def action_mark_interested(self) -> None:
        self.change_user_status("interested")

    def action_mark_rejected(self) -> None:
        self.change_user_status("rejected")

    def action_mark_applied(self) -> None:
        self.change_user_status("applied")

    def action_mark_archived(self) -> None:
        self.change_user_status("archived")

    def action_mark_new(self) -> None:
        self.change_user_status("new")

    def change_user_status(self, user_status: str) -> None:
        if self.mode != "jobs":
            self.notify("Switch to Jobs view first", severity="warning")
            return

        candidate_id = self.get_selected_candidate_id()

        if candidate_id is None:
            self.notify("No job selected", severity="warning")
            return

        try:
            set_user_status(candidate_id, user_status)
        except Exception as exc:
            self.notify(f"Could not update status: {exc}", severity="error")
            return

        self.refresh_jobs(self.query_one("#search", Input).value)
        self.show_job_detail(candidate_id)

        self.notify(
            f"Job #{candidate_id}: {USER_STATUS_LABELS[user_status]}",
            timeout=1.5,
        )

    def action_open_selected_url(self) -> None:
        if self.mode != "jobs":
            self.notify("Switch to Jobs view first", severity="warning")
            return

        candidate_id = self.get_selected_candidate_id()

        if candidate_id is None:
            self.notify("No job selected", severity="warning")
            return

        row = get_job_detail(candidate_id)

        if row is None:
            self.notify("Job not found", severity="error")
            return

        url = row["application_url"] or row["external_url"]

        if not url:
            self.notify("No URL available", severity="warning")
            return

        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            self.notify("Only http/https URLs can be opened", severity="error")
            return

        if self.pending_url == url:
            self.pending_url = None
            webbrowser.open(url)
            self.notify(f"Opened: {parsed.netloc}", timeout=2.0)
            return

        self.pending_url = url

        self.query_one("#detail", Static).update(
            Text.from_markup(
                "[bold yellow]URL confirmation[/bold yellow]\n\n"
                f"[bold]Job:[/bold] #{row['id']} "
                f"{escape(row['title'] or '(no title)')}\n"
                f"[bold]Host:[/bold] {escape(parsed.netloc or '?')}\n"
                f"[bold]URL:[/bold] {escape(url)}\n\n"
                "Press [bold]o[/bold] again to open this URL.\n"
                "Press any other key or select another job to cancel."
            )
        )

    async def on_unmount(self) -> None:
        self._pipeline_closing = True

        if self.pipeline is not None:
            await self.pipeline.stop()
            self.pipeline = None

if __name__ == "__main__":
    JobResearcherApp().run()