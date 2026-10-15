from __future__ import annotations

import sqlite3
from typing import Any

from jobresearcher.config import settings
import unicodedata
import json
import re
import hashlib
from functools import lru_cache

def init_pipeline_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS telegram_sources (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            source_kind TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            last_sync_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS message_analysis_queue (
            message_db_id INTEGER PRIMARY KEY
                REFERENCES messages(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_attempt_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_message_analysis_queue_status
        ON message_analysis_queue(status, next_attempt_at);
        """
    )

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(messages)"
        ).fetchall()
    }

    if "source_chat_id" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN source_chat_id INTEGER"
        )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_message
        ON messages(source_chat_id, message_id)
        WHERE source_chat_id IS NOT NULL
        """
    )

    rows = conn.execute(
        """
        SELECT m.id, m.text
        FROM messages m
        LEFT JOIN job_candidates jc
            ON jc.message_db_id = m.id
        LEFT JOIN message_analysis_queue q
            ON q.message_db_id = m.id
        WHERE jc.id IS NULL
          AND q.message_db_id IS NULL
          AND LENGTH(TRIM(COALESCE(m.text, ''))) > 0
        """
    ).fetchall()

    conn.executemany(
        """
        INSERT INTO message_analysis_queue (
            message_db_id,
            content_hash
        )
        VALUES (?, ?)
        """,
        [
            (
                row["id"],
                hashlib.sha256(
                    row["text"].encode("utf-8")
                ).hexdigest(),
            )
            for row in rows
        ],
    )

def search_normalize(value: Any) -> str:
    if value is None:
        return ""

    return unicodedata.normalize(
        "NFKC",
        str(value),
    ).casefold()

def search_terms(query: Any) -> tuple[str, ...]:
    normalized = search_normalize(query)
    return tuple(dict.fromkeys(normalized.split()))


@lru_cache(maxsize=512)
def _search_pattern(term: str) -> re.Pattern:
    return re.compile(
        rf"(?<!\w){re.escape(term)}(?!\w)"
    )


@lru_cache(maxsize=512)
def _prefix_search_pattern(term: str) -> re.Pattern:
    return re.compile(
        rf"(?<!\w){re.escape(term)}\w*(?!\w)"
    )


def search_match(value: Any, term: Any) -> int:
    normalized_term = search_normalize(term).strip()

    if not normalized_term:
        return 0

    normalized_text = search_normalize(value)

    if _search_pattern(normalized_term).search(normalized_text):
        return 1

    allow_prefix = (
        len(normalized_term) >= 3
        and normalized_term.isalnum()
    )

    if (
        allow_prefix
        and _prefix_search_pattern(normalized_term).search(
            normalized_text
        )
    ):
        return 1

    return 0


def get_pending_fetch_jobs(
    limit: int = 10,
    max_attempts: int = 3,
    include_retries: bool = True,
) -> list[sqlite3.Row]:
    if limit < 1 or max_attempts < 1:
        raise ValueError("limit and max_attempts must be positive")

    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT
                id,
                external_url,
                raw_telegram_text,
                posted_at
            FROM job_candidates
            WHERE external_url IS NOT NULL
              AND LENGTH(TRIM(external_url)) > 0
              AND analysis_status != 'rejected_non_job'
              AND fetch_attempts < ?
              AND (
                  fetch_status = 'pending'
                  OR (
                      ? = 1
                      AND fetch_status = 'failed'
                      AND fetch_retryable = 1
                      AND fetch_next_attempt_at IS NOT NULL
                      AND datetime(fetch_next_attempt_at)
                          <= datetime('now')
                  )
              )
            ORDER BY
                CASE WHEN fetch_status = 'pending' THEN 0 ELSE 1 END,
                id ASC
            LIMIT ?
            """,
            (max_attempts, int(include_retries), limit),
        ).fetchall()
    finally:
        conn.close()


def save_fetch_job_result(
    candidate_id: int,
    expected_url: str,
    expected_text: str,
    fetch_status: str,
    page_text: str | None = None,
    final_url: str | None = None,
    job_status: str = "unknown",
    error: str | None = None,
    retryable: bool = False,
    short_text: bool = False,
) -> bool:
    if fetch_status not in {"success", "failed", "skipped"}:
        raise ValueError(f"Invalid fetch status: {fetch_status}")

    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT
                jc.external_url,
                jc.raw_telegram_text,
                jc.page_text,
                jc.fetch_attempts,
                m.text AS current_message_text
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE jc.id = ?
            """,
            (candidate_id,),
        ).fetchone()

        if (
            row is None
            or row["external_url"] != expected_url
            or (row["raw_telegram_text"] or "") != expected_text
            or (row["current_message_text"] or "") != expected_text
        ):
            conn.rollback()
            return False

        attempts = row["fetch_attempts"] + 1
        should_retry = fetch_status == "failed" and retryable
        delay_seconds = min(300 * (2 ** (attempts - 1)), 3600)

        success = fetch_status == "success"
        new_page_text = page_text if success else row["page_text"]

        page_changed = (
            success
            and (row["page_text"] or "") != (page_text or "")
        )

        conn.execute(
            """
            UPDATE job_candidates
            SET
                fetch_status = ?,
                page_text = ?,
                external_url = ?,
                external_job_status = ?,
                fetch_attempts = ?,
                fetch_last_error = ?,
                fetch_retryable = ?,
                fetch_short_text = ?,
                fetch_last_attempt_at = CURRENT_TIMESTAMP,
                fetch_next_attempt_at = CASE
                    WHEN ? = 1
                    THEN datetime('now', ?)
                    ELSE NULL
                END,
                analysis_status = CASE
                    WHEN ? = 1 THEN 'rules_complete'
                    ELSE analysis_status
                END,
                content_type = CASE
                    WHEN ? = 1 THEN NULL
                    ELSE content_type
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                fetch_status,
                new_page_text,
                final_url if success and final_url else expected_url,
                job_status if success else "unknown",
                attempts,
                error[:2000] if error else None,
                int(should_retry),
                int(short_text) if success else 0,
                int(should_retry),
                f"+{delay_seconds} seconds",
                int(page_changed),
                int(page_changed),
                candidate_id,
            ),
        )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def init_enrichment_schema(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(job_candidates)"
        ).fetchall()
    }

    additions = {
        "occupation_terms": "TEXT NOT NULL DEFAULT '[]'",
        "skill_terms": "TEXT NOT NULL DEFAULT '[]'",
        "esco_candidates": "TEXT NOT NULL DEFAULT '{}'",
        "esco_classification_status": "TEXT NOT NULL DEFAULT 'pending'",
        "extraction_meta": "TEXT NOT NULL DEFAULT '{}'",
        "enrichment_version": "TEXT",
        "enrichment_input_hash": "TEXT",
        "enrichment_attempt_version": "TEXT",
        "enrichment_attempt_hash": "TEXT",
        "enrichment_attempts": "INTEGER NOT NULL DEFAULT 0",
        "enrichment_last_error": "TEXT",
        "enrichment_next_attempt_at": "TEXT",
        "enrichment_updated_at": "TEXT",
    }

    for name, definition in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE job_candidates "
                f"ADD COLUMN {name} {definition}"
            )
        
def query_relevance(
    query: Any,
    title: Any,
    skills: Any,
    company: Any,
    summary: Any,
    telegram_text: Any,
    page_text: Any,
    location_text: Any,
) -> float:
    terms = search_terms(query)

    if not terms:
        return 0.0

    skills_text = skills or ""

    if isinstance(skills_text, str):
        try:
            parsed_skills = json.loads(skills_text)
        except (json.JSONDecodeError, TypeError):
            parsed_skills = None

        if isinstance(parsed_skills, list):
            skills_text = " ".join(
                str(skill) for skill in parsed_skills
            )

    fields = (
        (title, 100),
        (skills_text, 80),
        (company, 50),
        (summary, 30),
        (telegram_text, 20),
        (page_text, 20),
        (location_text, 10),
    )

    total = 0

    for term in terms:
        best_match = max(
            (
                weight
                for value, weight in fields
                if search_match(value, term)
            ),
            default=0,
        )
        total += best_match

    return round(total / len(terms), 2)

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    conn.create_function(
        "search_normalize",
        1,
        search_normalize,
        deterministic=True,
    )

    conn.create_function(
        "search_match",
        2,
        search_match,
        deterministic=True,
    )

    conn.create_function(
        "query_relevance",
        8,
        query_relevance,
        deterministic=True,
    )

    return conn

def init_postprocessing_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_postprocessing (
            candidate_id INTEGER NOT NULL
                REFERENCES job_candidates(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(candidate_id, stage)
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_postprocessing_stage
        ON candidate_postprocessing(stage);
        """
    )

def init_db() -> None:
    conn = get_conn()

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                text TEXT,
                date TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_date
            ON messages(date DESC);

            CREATE INDEX IF NOT EXISTS idx_messages_channel
            ON messages(channel);

            CREATE TABLE IF NOT EXISTS job_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                message_db_id INTEGER NOT NULL UNIQUE
                REFERENCES messages(id) ON DELETE CASCADE,

                source_channel TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                telegram_url TEXT,
                posted_at TIMESTAMP NOT NULL,

                source_type TEXT NOT NULL DEFAULT 'telegram_only',
                raw_telegram_text TEXT NOT NULL,

                external_url TEXT,
                page_text TEXT,
                fetch_status TEXT NOT NULL DEFAULT 'not_needed',

                title TEXT,
                company TEXT,
                employment_type TEXT NOT NULL DEFAULT 'unknown',
                seniority TEXT NOT NULL DEFAULT 'unknown',

                location_type TEXT NOT NULL DEFAULT 'unknown',
                location_text TEXT,
                eligible_regions TEXT NOT NULL DEFAULT '[]',
                timezone_text TEXT,

                salary_min INTEGER,
                salary_max INTEGER,
                salary_currency TEXT,
                salary_period TEXT NOT NULL DEFAULT 'unknown',
                salary_text TEXT,
                equity_text TEXT,

                degree_status TEXT NOT NULL DEFAULT 'unknown',
                experience_text TEXT,
                skills TEXT NOT NULL DEFAULT '[]',
                keywords TEXT NOT NULL DEFAULT '[]',
                description_summary TEXT,

                application_url TEXT,
                contact_email TEXT,
                contact_telegram TEXT,
                contact_discord TEXT,
                contact_text TEXT,
                application_method TEXT NOT NULL DEFAULT 'unknown',
                deadline TIMESTAMP,

                rule_score INTEGER NOT NULL DEFAULT 0,
                llm_score REAL,
                final_score REAL NOT NULL DEFAULT 0,
                analysis_status TEXT NOT NULL DEFAULT 'pending',

                application_status TEXT NOT NULL DEFAULT 'new',
                notes TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_score
            ON job_candidates(final_score DESC);

            CREATE INDEX IF NOT EXISTS idx_candidates_status
            ON job_candidates(application_status);

            CREATE INDEX IF NOT EXISTS idx_candidates_location
            ON job_candidates(location_type);

            CREATE INDEX IF NOT EXISTS idx_candidates_degree
            ON job_candidates(degree_status);

            CREATE INDEX IF NOT EXISTS idx_candidates_source_channel
            ON job_candidates(source_channel);

            CREATE INDEX IF NOT EXISTS idx_candidates_posted_at
            ON job_candidates(posted_at DESC);

            CREATE TABLE IF NOT EXISTS job_user_state (
                candidate_id INTEGER PRIMARY KEY
                REFERENCES job_candidates(id) ON DELETE CASCADE,

                user_status TEXT NOT NULL DEFAULT 'new',
                notes TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_job_user_state_status
            ON job_user_state(user_status);
            """
        )

        existing_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(job_candidates)"
            ).fetchall()
        }

        if "external_job_status" not in existing_columns:
            conn.execute(
                """
                ALTER TABLE job_candidates
                ADD COLUMN external_job_status TEXT NOT NULL DEFAULT 'unknown'
                """
            )

        if "content_type" not in existing_columns:
            conn.execute(
                """
                ALTER TABLE job_candidates
                ADD COLUMN content_type TEXT
                """
            )

        if "is_multi_job" not in existing_columns:
            conn.execute(
                """
                ALTER TABLE job_candidates
                ADD COLUMN is_multi_job INTEGER NOT NULL DEFAULT 0
                """
            )
        if "duplicate_of" not in existing_columns:
            conn.execute(
                """
                ALTER TABLE job_candidates
                ADD COLUMN duplicate_of INTEGER
                REFERENCES job_candidates(id) ON DELETE SET NULL
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_candidates_duplicate_of
            ON job_candidates(duplicate_of)
            """
        )

        init_pipeline_schema(conn)
        init_fetch_schema(conn)
        init_enrichment_schema(conn)
        init_postprocessing_schema(conn)
        conn.commit()

    finally:
        conn.close()


def save_message(
    channel: str,
    message_id: int,
    text: str,
    date: Any,
) -> bool:
    """Save a raw Telegram post; return False if it already exists."""
    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            INSERT INTO messages (channel, message_id, text, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO NOTHING
            """,
            (channel, message_id, text, date),
        )

        conn.commit()
        return cursor.rowcount > 0

    finally:
        conn.close()


def get_recent_messages(
    query: str = "",
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Search raw Telegram posts; every query term must be present."""
    conn = get_conn()

    try:
        normalized_query = " ".join(query.lower().split())

        sql = """
            SELECT id, channel, message_id, text, date
            FROM messages
            WHERE 1 = 1
        """
        params: list[Any] = []

        if normalized_query:
            for term in normalized_query.split():
                sql += """
                    AND (
                        LOWER(COALESCE(text, '')) LIKE ?
                        OR LOWER(COALESCE(channel, '')) LIKE ?
                    )
                """
                pattern = f"%{term}%"
                params.extend([pattern, pattern])

        sql += " ORDER BY datetime(date) DESC LIMIT ?"
        params.append(limit)

        return conn.execute(sql, params).fetchall()

    finally:
        conn.close()


def get_message_by_id(row_id: int) -> sqlite3.Row | None:
    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT id, channel, message_id, text, date
            FROM messages
            WHERE id = ?
            """,
            (row_id,),
        ).fetchone()

    finally:
        conn.close()


def get_all_messages() -> list[sqlite3.Row]:
    """Return all non-empty raw messages for batch analysis."""
    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT id, channel, message_id, text, date
            FROM messages
            WHERE text IS NOT NULL
              AND TRIM(text) != ''
            ORDER BY datetime(date) DESC
            """
        ).fetchall()

    finally:
        conn.close()


def upsert_job_candidate(
    candidate: dict[str, Any],
    expected_content_hash: str | None = None,
) -> bool:
   
    columns = (
        "message_db_id",
        "source_channel",
        "telegram_message_id",
        "telegram_url",
        "posted_at",
        "source_type",
        "raw_telegram_text",
        "external_url",
        "fetch_status",
        "title",
        "company",
        "employment_type",
        "seniority",
        "location_type",
        "location_text",
        "eligible_regions",
        "timezone_text",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "salary_text",
        "degree_status",
        "skills",
        "keywords",
        "application_url",
        "contact_email",
        "contact_telegram",
        "contact_discord",
        "contact_text",
        "application_method",
        "rule_score",
        "final_score",
        "analysis_status",
    )

    missing = [name for name in columns if name not in candidate]

    if missing:
        raise ValueError(
            "Missing candidate fields: " + ", ".join(missing)
        )

    values = {name: candidate[name] for name in columns}
    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        if expected_content_hash is not None:
            message = conn.execute(
                """
                SELECT text
                FROM messages
                WHERE id = ?
                """,
                (values["message_db_id"],),
            ).fetchone()

            queue = conn.execute(
                """
                SELECT content_hash
                FROM message_analysis_queue
                WHERE message_db_id = ?
                """,
                (values["message_db_id"],),
            ).fetchone()

            if message is None or queue is None:
                conn.rollback()
                return False

            current_hash = hashlib.sha256(
                (message["text"] or "").encode("utf-8")
            ).hexdigest()

            candidate_hash = hashlib.sha256(
                (values["raw_telegram_text"] or "").encode("utf-8")
            ).hexdigest()

            if (
                current_hash != expected_content_hash
                or candidate_hash != expected_content_hash
                or queue["content_hash"] != expected_content_hash
            ):
                conn.rollback()
                return False

        existing = conn.execute(
            """
            SELECT *
            FROM job_candidates
            WHERE message_db_id = ?
            """,
            (values["message_db_id"],),
        ).fetchone()

        if existing is None:
            names = ", ".join(columns)
            bindings = ", ".join(f":{name}" for name in columns)

            conn.execute(
                f"""
                INSERT INTO job_candidates ({names})
                VALUES ({bindings})
                """,
                values,
            )

        else:
            old_text = existing["raw_telegram_text"] or ""
            new_text = values["raw_telegram_text"] or ""
            same_text = old_text == new_text

            protected = existing["analysis_status"] in {
                "enriched",
                "needs_review",
                "rejected_non_job",
            }

            if same_text and protected:
                conn.execute(
                    """
                    UPDATE job_candidates
                    SET
                        source_channel = ?,
                        telegram_message_id = ?,
                        telegram_url = ?,
                        posted_at = ?
                    WHERE id = ?
                    """,
                    (
                        values["source_channel"],
                        values["telegram_message_id"],
                        values["telegram_url"],
                        values["posted_at"],
                        existing["id"],
                    ),
                )

            else:
                same_url = (
                    (existing["external_url"] or "").strip()
                    == (values["external_url"] or "").strip()
                )

                update_columns = [
                    name
                    for name in columns
                    if name != "message_db_id"
                ]

                if same_url:
                    update_columns.remove("fetch_status")

                assignments = ", ".join(
                    f"{name} = :{name}"
                    for name in update_columns
                )

                extra_assignments = []

                if not same_text:
                    values["analysis_status"] = "rules_complete"
                    extra_assignments.extend([
                        "content_type = NULL",
                        "llm_score = NULL",
                        "duplicate_of = NULL",
                    ])

                if not same_url:
                    extra_assignments.extend([
                        "page_text = NULL",
                        "external_job_status = 'unknown'",
                    ])

                if extra_assignments:
                    assignments += ", " + ", ".join(extra_assignments)

                values["candidate_id"] = existing["id"]

                conn.execute(
                    f"""
                    UPDATE job_candidates
                    SET
                        {assignments},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :candidate_id
                    """,
                    values,
                )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def init_fetch_schema(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(job_candidates)"
        ).fetchall()
    }

    additions = {
        "fetch_attempts": "INTEGER NOT NULL DEFAULT 0",
        "fetch_last_error": "TEXT",
        "fetch_next_attempt_at": "TEXT",
        "fetch_last_attempt_at": "TEXT",
        "fetch_retryable": "INTEGER NOT NULL DEFAULT 0",
        "fetch_short_text": "INTEGER NOT NULL DEFAULT 0",
    }

    for name, definition in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE job_candidates "
                f"ADD COLUMN {name} {definition}"
            )

def get_stats() -> dict[str, int]:
    conn = get_conn()

    try:
        total_messages = conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        total_channels = conn.execute(
            "SELECT COUNT(DISTINCT channel) FROM messages"
        ).fetchone()[0]

        today_messages = conn.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE date(date) = date('now', 'localtime')
            """
        ).fetchone()[0]

        total_candidates = conn.execute(
            "SELECT COUNT(*) FROM job_candidates"
        ).fetchone()[0]

        relevant_candidates = conn.execute(
            """
            SELECT COUNT(*)
            FROM job_candidates
            WHERE final_score >= 20
            """
        ).fetchone()[0]

        return {
            "total_messages": total_messages,
            "total_channels": total_channels,
            "today_messages": today_messages,
            "total_candidates": total_candidates,
            "relevant_candidates": relevant_candidates,
        }

    finally:
        conn.close()


def get_top_candidates(
    min_score: float = 0,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Return candidates ordered by score and date."""
    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT
                id,
                message_db_id,
                source_channel,
                telegram_message_id,
                telegram_url,
                posted_at,

                title,
                company,
                employment_type,
                seniority,

                location_type,
                location_text,
                eligible_regions,
                timezone_text,

                salary_min,
                salary_max,
                salary_currency,
                salary_period,
                salary_text,

                degree_status,
                skills,
                keywords,

                application_url,
                contact_email,
                contact_telegram,
                contact_discord,
                contact_text,
                application_method,

                rule_score,
                llm_score,
                final_score,
                analysis_status,

                application_status,
                notes,

                raw_telegram_text,
                external_url,
                fetch_status
            FROM job_candidates
            WHERE final_score >= ?
            ORDER BY final_score DESC, datetime(posted_at) DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()

    finally:
        conn.close()


def search_candidates(
    query: str = "",
    min_score: float = 20,
    remote_only: bool = False,
    no_degree_only: bool = False,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """
    Search analyzed candidates.

    Every query term must appear in at least one of these fields:
    title, company, skills JSON, raw text, or source channel.
    """
    conn = get_conn()

    try:
        sql = """
            SELECT
                id,
                message_db_id,
                source_channel,
                telegram_message_id,
                telegram_url,
                posted_at,

                title,
                company,
                employment_type,
                seniority,

                location_type,
                location_text,
                eligible_regions,
                timezone_text,

                salary_min,
                salary_max,
                salary_currency,
                salary_period,
                salary_text,

                degree_status,
                skills,
                keywords,

                application_url,
                contact_email,
                contact_telegram,
                contact_discord,
                contact_text,
                application_method,

                rule_score,
                llm_score,
                final_score,
                analysis_status,

                application_status,
                notes,

                raw_telegram_text,
                external_url,
                fetch_status
            FROM job_candidates
            WHERE final_score >= ?
        """
        params: list[Any] = [min_score]

        if remote_only:
            sql += " AND location_type = 'remote'"

        if no_degree_only:
            sql += " AND degree_status != 'required'"

        normalized_query = " ".join(query.lower().split())

        if normalized_query:
            for term in normalized_query.split():
                sql += """
                    AND (
                        LOWER(COALESCE(title, '')) LIKE ?
                        OR LOWER(COALESCE(company, '')) LIKE ?
                        OR LOWER(COALESCE(skills, '')) LIKE ?
                        OR LOWER(COALESCE(raw_telegram_text, '')) LIKE ?
                        OR LOWER(COALESCE(source_channel, '')) LIKE ?
                    )
                """
                pattern = f"%{term}%"
                params.extend([pattern] * 5)

        sql += """
            ORDER BY
                final_score DESC,
                datetime(posted_at) DESC
            LIMIT ?
        """
        params.append(limit)

        return conn.execute(sql, params).fetchall()

    finally:
        conn.close()


def get_inbox_jobs(
    query: str = "",
    user_status: str = "active",
    limit: int = 300,
    *,
    include_multi_job: bool = False,
    include_duplicates: bool = False,
    location_type: str | None = None,
    location_query: str = "",
    seniority: str | None = None,
    employment_type: str | None = None,
    degree_status: str | None = None,
    salary_known: bool = False,
    page_available: bool = False,
    sort_by: str = "auto",
) -> list[sqlite3.Row]:
    """
    Read the inbox without changing stored data.

    Every query term must match at least one field. Matching uses
    word boundaries, so 'rust' does not match 'trust'.

    relevance_score is calculated for the current query.
    final_score remains the previously stored score.

    sort_by:
      auto      -> relevance with a query, newest first without one
      relevance -> relevance, or newest first without a query
      score     -> stored final_score
      recent / oldest / company / title / quality / interested
                 -> explicit sort order
    """
    allowed_user_statuses = {
        "active",
        "all",
        "new",
        "interested",
        "rejected",
        "applied",
        "archived",
    }

    allowed_filters = {
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

    selected_filters = {
        "location_type": location_type,
        "seniority": seniority,
        "employment_type": employment_type,
        "degree_status": degree_status,
    }

    sort_orders = {
        "relevance": """
            relevance_score DESC,
            datetime(jc.posted_at) IS NULL,
            datetime(jc.posted_at) DESC,
            jc.id DESC
        """,
        "score": """
            jc.final_score DESC,
            datetime(jc.posted_at) DESC,
            jc.id DESC
        """,
        "recent": """
            datetime(jc.posted_at) IS NULL,
            datetime(jc.posted_at) DESC,
            jc.id DESC
        """,
        "oldest": """
            datetime(jc.posted_at) IS NULL,
            datetime(jc.posted_at) ASC,
            jc.id ASC
        """,
        "company": """
            NULLIF(TRIM(jc.company), '') IS NULL,
            search_normalize(jc.company) ASC,
            search_normalize(jc.title) ASC,
            jc.id DESC
        """,
        "title": """
            NULLIF(TRIM(jc.title), '') IS NULL,
            search_normalize(jc.title) ASC,
            search_normalize(jc.company) ASC,
            jc.id DESC
        """,
        "quality": """
            CASE
                WHEN jc.analysis_status = 'enriched' THEN 0
                ELSE 1
            END,
            datetime(jc.posted_at) DESC,
            jc.id DESC
        """,
        "interested": """
            CASE
                WHEN COALESCE(us.user_status, 'new') = 'interested'
                THEN 0
                ELSE 1
            END,
            datetime(jc.posted_at) DESC,
            jc.id DESC
        """,
    }

    if user_status not in allowed_user_statuses:
        raise ValueError(
            f"Invalid user status: {user_status}"
        )

    if sort_by not in {"auto", *sort_orders}:
        raise ValueError(f"Invalid sort: {sort_by}")

    if limit < 1:
        raise ValueError("limit must be positive")

    for field, value in selected_filters.items():
        if value is not None and value not in allowed_filters[field]:
            raise ValueError(f"Invalid {field}: {value}")

    terms = search_terms(query)
    normalized_query = " ".join(terms)

    effective_sort = sort_by

    if sort_by == "auto":
        effective_sort = "relevance" if terms else "recent"
    elif sort_by == "relevance" and not terms:
        effective_sort = "recent"

    conn = get_conn()

    try:
        sql = """
            SELECT
                jc.id,
                jc.final_score,
                jc.title,
                jc.company,
                jc.seniority,
                jc.employment_type,
                jc.location_type,
                jc.location_text,
                jc.salary_min,
                jc.salary_max,
                jc.salary_currency,
                jc.salary_period,
                jc.degree_status,
                jc.skills,
                jc.analysis_status,
                jc.content_type,
                jc.is_multi_job,
                jc.duplicate_of,
                jc.external_url,
                jc.application_url,
                jc.posted_at,

                query_relevance(
                    ?,
                    jc.title,
                    jc.skills,
                    jc.company,
                    jc.description_summary,
                    jc.raw_telegram_text,
                    jc.page_text,
                    jc.location_text
                ) AS relevance_score,

                CASE
                    WHEN LENGTH(
                        TRIM(COALESCE(jc.page_text, ''))
                    ) > 0
                    THEN 1
                    ELSE 0
                END AS has_page_text,

                COALESCE(us.user_status, 'new') AS user_status

            FROM job_candidates jc
            LEFT JOIN job_user_state us
                ON us.candidate_id = jc.id

            WHERE jc.content_type = 'job'
              AND jc.analysis_status IN (
                  'enriched',
                  'needs_review'
              )
        """

        params: list[Any] = [normalized_query]

        if not include_multi_job:
            sql += " AND COALESCE(jc.is_multi_job, 0) = 0"

        if not include_duplicates:
            sql += " AND jc.duplicate_of IS NULL"

        if user_status == "active":
            sql += """
                AND COALESCE(us.user_status, 'new')
                    IN ('new', 'interested')
            """
        elif user_status != "all":
            sql += """
                AND COALESCE(us.user_status, 'new') = ?
            """
            params.append(user_status)

        for field, value in selected_filters.items():
            if value is not None:
                sql += f" AND jc.{field} = ?"
                params.append(value)

        if salary_known:
            sql += """
                AND (
                    COALESCE(jc.salary_min, 0) > 0
                    OR COALESCE(jc.salary_max, 0) > 0
                )
            """

        if page_available:
            sql += """
                AND LENGTH(
                    TRIM(COALESCE(jc.page_text, ''))
                ) > 0
            """

        location_term = search_normalize(
            location_query
        ).strip()

        if location_term:
            sql += """
                AND instr(
                    search_normalize(jc.location_text),
                    ?
                ) > 0
            """
            params.append(location_term)

        search_fields = (
            "title",
            "skills",
            "company",
            "description_summary",
            "raw_telegram_text",
            "page_text",
            "location_text",
        )

        for term in terms:
            conditions = [
                f"search_match(jc.{field}, ?) = 1"
                for field in search_fields
            ]

            sql += " AND (" + " OR ".join(conditions) + ")"
            params.extend([term] * len(search_fields))

        sql += (
            f" ORDER BY {sort_orders[effective_sort]}"
            " LIMIT ?"
        )
        params.append(limit)

        return conn.execute(sql, params).fetchall()

    finally:
        conn.close()

def get_job_detail(candidate_id: int) -> sqlite3.Row | None:
    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT
                jc.*,
                m.text AS message_text,
                COALESCE(us.user_status, 'new') AS user_status,
                us.notes AS user_notes
            FROM job_candidates jc
            LEFT JOIN messages m
                ON m.id = jc.message_db_id
            LEFT JOIN job_user_state us
                ON us.candidate_id = jc.id
            WHERE jc.id = ?
            """,
            (candidate_id,),
        ).fetchone()

    finally:
        conn.close()


def set_user_status(
    candidate_id: int,
    user_status: str,
    notes: str | None = None,
) -> None:
    allowed = {
        "new",
        "interested",
        "rejected",
        "applied",
        "archived",
    }

    if user_status not in allowed:
        raise ValueError(f"Invalid user status: {user_status}")

    conn = get_conn()

    try:
        conn.execute(
            """
            INSERT INTO job_user_state (
                candidate_id,
                user_status,
                notes,
                updated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                user_status = excluded.user_status,
                notes = COALESCE(
                    excluded.notes,
                    job_user_state.notes
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            (candidate_id, user_status, notes),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def get_user_status(candidate_id: int) -> str:
    conn = get_conn()

    try:
        row = conn.execute(
            """
            SELECT user_status
            FROM job_user_state
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()

        return row["user_status"] if row else "new"

    finally:
        conn.close()


def get_candidates_to_fetch(
    min_score: float = 0,
    limit: int = 25,
) -> list[sqlite3.Row]:
    """
    Return pages waiting to be fetched, regardless of the old score.

    min_score is kept for compatibility and is not applied.
    """
    if limit < 1:
        raise ValueError("limit must be positive")

    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT
                id,
                external_url,
                final_score,
                source_channel,
                raw_telegram_text
            FROM job_candidates
            WHERE external_url IS NOT NULL
              AND TRIM(external_url) != ''
              AND fetch_status = 'pending'
              AND analysis_status != 'rejected_non_job'
            ORDER BY
                datetime(posted_at) IS NULL,
                datetime(posted_at) DESC,
                id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
def update_fetch_result(
    candidate_id: int,
    fetch_status: str,
    page_text: str | None = None,
    external_url: str | None = None,
    external_job_status: str = "unknown",
) -> None:
    """Save the fetch result, extracted text, final URL, and job status."""
    conn = get_conn()

    try:
        conn.execute(
            """
            UPDATE job_candidates
            SET
                fetch_status = ?,
                page_text = ?,
                external_url = COALESCE(?, external_url),
                external_job_status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                fetch_status,
                page_text,
                external_url,
                external_job_status,
                candidate_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()

def set_multi_job(candidate_id: int, is_multi_job: bool) -> None:
    """Update only the candidate's multi-job flag."""
    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            UPDATE job_candidates
            SET
                is_multi_job = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(is_multi_job), candidate_id),
        )

        if cursor.rowcount == 0:
            raise ValueError(f"Candidate {candidate_id} not found")

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def upsert_telegram_source(
    chat_id: int,
    title: str,
    username: str | None,
    source_kind: str,
    enabled: bool = False,
) -> None:
    if source_kind not in {"group", "channel"}:
        raise ValueError("source_kind must be 'group' or 'channel'")

    conn = get_conn()

    try:
        conn.execute(
            """
            INSERT INTO telegram_sources (
                chat_id,
                title,
                username,
                source_kind,
                enabled
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                source_kind = excluded.source_kind,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                chat_id,
                title,
                username,
                source_kind,
                int(enabled),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_telegram_sources(
    enabled_only: bool = False,
) -> list[sqlite3.Row]:
    conn = get_conn()

    try:
        sql = "SELECT * FROM telegram_sources"

        if enabled_only:
            sql += " WHERE enabled = 1"

        sql += " ORDER BY search_normalize(title), chat_id"
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def set_telegram_source_enabled(
    chat_id: int,
    enabled: bool,
) -> None:
    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            UPDATE telegram_sources
            SET enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ?
            """,
            (int(enabled), chat_id),
        )

        if cursor.rowcount == 0:
            raise ValueError(f"Telegram source not found: {chat_id}")

        conn.commit()
    finally:
        conn.close()


def update_telegram_source_cursor(
    chat_id: int,
    last_message_id: int,
) -> None:
    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            UPDATE telegram_sources
            SET
                last_message_id = MAX(last_message_id, ?),
                last_sync_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ?
            """,
            (last_message_id, chat_id),
        )

        if cursor.rowcount == 0:
            raise ValueError(f"Telegram source not found: {chat_id}")

        conn.commit()
    finally:
        conn.close()

def save_telegram_message(
    chat_id: int,
    channel: str,
    message_id: int,
    text: str,
    date: Any,
    legacy_channel: str | None = None,
) -> bool:
    
    text = text or ""
    content_hash = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT id, text
            FROM messages
            WHERE source_chat_id = ?
              AND message_id = ?
            """,
            (chat_id, message_id),
        ).fetchone()

        if existing is None and legacy_channel:
            existing = conn.execute(
                """
                SELECT id, text
                FROM messages
                WHERE source_chat_id IS NULL
                  AND channel = ?
                  AND message_id = ?
                """,
                (legacy_channel, message_id),
            ).fetchone()

        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    channel,
                    message_id,
                    text,
                    date,
                    source_chat_id
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (channel, message_id, text, date, chat_id),
            )
            message_db_id = cursor.lastrowid
            changed = True

        else:
            message_db_id = existing["id"]
            changed = (existing["text"] or "") != text

            conn.execute(
                """
                UPDATE messages
                SET
                    channel = ?,
                    text = ?,
                    date = ?,
                    source_chat_id = ?
                WHERE id = ?
                """,
                (channel, text, date, chat_id, message_db_id),
            )

        if changed:
            conn.execute(
                """
                INSERT INTO message_analysis_queue (
                    message_db_id,
                    content_hash,
                    status
                )
                VALUES (?, ?, 'pending')
                ON CONFLICT(message_db_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = 'pending',
                    attempts = 0,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (message_db_id, content_hash),
            )

            conn.execute(
                """
                UPDATE job_candidates
                SET
                    raw_telegram_text = ?,
                    analysis_status = 'pending',
                    content_type = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_db_id = ?
                """,
                (text, message_db_id),
            )

        conn.commit()
        return changed

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_pending_message_analysis(
    limit: int = 50,
    max_attempts: int = 5,
) -> list[sqlite3.Row]:
    if limit < 1 or max_attempts < 1:
        raise ValueError("limit and max_attempts must be positive")

    conn = get_conn()

    try:
        return conn.execute(
            """
            SELECT
                m.*,
                q.content_hash,
                q.attempts AS analysis_attempts
            FROM message_analysis_queue q
            JOIN messages m ON m.id = q.message_db_id
            WHERE q.status IN ('pending', 'failed')
              AND q.attempts < ?
              AND (
                  q.next_attempt_at IS NULL
                  OR datetime(q.next_attempt_at) <= datetime('now')
              )
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (max_attempts, limit),
        ).fetchall()
    finally:
        conn.close()


def finish_message_analysis(
    message_db_id: int,
    content_hash: str,
    error: str | None = None,
) -> bool:
    """
    Update the queue only if the content has not changed
    during processing.
    """
    conn = get_conn()

    try:
        if error is None:
            cursor = conn.execute(
                """
                UPDATE message_analysis_queue
                SET
                    status = 'done',
                    last_error = NULL,
                    next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_db_id = ?
                  AND content_hash = ?
                """,
                (message_db_id, content_hash),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE message_analysis_queue
                SET
                    status = 'failed',
                    attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = datetime('now', '+5 minutes'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_db_id = ?
                  AND content_hash = ?
                """,
                (error[:2000], message_db_id, content_hash),
            )

        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()