from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
ESCO_MODEL = "http://data.europa.eu/esco/model#"
ESCO_BASE = "http://data.europa.eu/esco/"

TYPE_URIS = {
    "occupation": ESCO_MODEL + "Occupation",
    "skill": ESCO_MODEL + "Skill",
}

URI_PREFIXES = {
    "occupation": ESCO_BASE + "occupation/",
    "skill": ESCO_BASE + "skill/",
}

VALID_KINDS = {"occupation", "skill"}

MATCH_TYPES = {
    0: "exact",
    1: "word_exact",
    2: "prefix",
    3: "word_prefix",
}


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


@lru_cache(maxsize=1024)
def _label_pattern(query: str, prefix: bool) -> re.Pattern:
    escaped = re.escape(query)

    if prefix:
        return re.compile(
            rf"(?<!\w){escaped}\w*(?!\w)"
        )

    return re.compile(
        rf"(?<!\w){escaped}(?!\w)"
    )


def label_match_rank(
    value: str,
    query: str,
    allow_prefix: int,
) -> int:
    """
    0: label exactly matches the query
    1: complete word or phrase within the label
    2: prefix at the start of the label
    3: prefix of a word within the label
    4: no match
    """
    text = normalize(value or "")
    query = normalize(query or "").strip()

    if not query:
        return 4

    if text == query:
        return 0

    if _label_pattern(query, prefix=False).search(text):
        return 1

    use_prefix = (
        bool(allow_prefix)
        and len(query) >= 3
        and query[-1].isalnum()
    )

    if not use_prefix:
        return 4

    match = _label_pattern(query, prefix=True).search(text)

    if match is None:
        return 4

    return 2 if match.start() == 0 else 3


@dataclass(frozen=True)
class EscoLabel:
    language: str
    text: str
    label_type: str


@dataclass(frozen=True)
class EscoMatch:
    uri: str
    kind: str
    preferred_label: str
    preferred_language: str
    matched_label: str
    matched_language: str
    matched_label_type: str
    match_type: str
    kind_basis: str
    obsolete: bool


class EscoCatalog:
    def __init__(
        self,
        database: str | Path | None = None,
    ) -> None:
        configured = database or os.getenv(
            "ESCO_DB_PATH",
            "data/esco.sqlite3",
        )

        self.database = Path(configured).resolve()

        if not self.database.is_file():
            raise FileNotFoundError(
                f"ESCO catalog not found: {self.database}"
            )

        metadata = self.metadata()

        if metadata.get("status") != "complete":
            raise RuntimeError(
                "The ESCO catalog import is incomplete"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.database.as_uri() + "?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row

        try:
            conn.execute("PRAGMA query_only = ON")

            conn.create_function(
                "esco_label_rank",
                3,
                label_match_rank,
                deterministic=True,
            )

            yield conn

        finally:
            conn.close()

    def metadata(self) -> dict[str, str]:
        with self._connection() as conn:
            return {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM metadata"
                )
            }

    @staticmethod
    def _languages(
        languages: tuple[str, ...] | list[str] | None,
    ) -> tuple[str, ...]:
        if languages is None:
            return ()

        return tuple(
            dict.fromkeys(
                language.strip().casefold()
                for language in languages
                if language.strip()
            )
        )

    @staticmethod
    def _kind_sql(alias: str = "l") -> str:
        return f"""
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM rdf_triples AS t
                    WHERE t.subject_kind = {alias}.concept_kind
                      AND t.subject = {alias}.concept_uri
                      AND t.predicate = '{RDF_TYPE}'
                      AND t.object_kind = 'uri'
                      AND t.object = '{TYPE_URIS["occupation"]}'
                )
                THEN 'occupation'

                WHEN EXISTS (
                    SELECT 1
                    FROM rdf_triples AS t
                    WHERE t.subject_kind = {alias}.concept_kind
                      AND t.subject = {alias}.concept_uri
                      AND t.predicate = '{RDF_TYPE}'
                      AND t.object_kind = 'uri'
                      AND t.object = '{TYPE_URIS["skill"]}'
                )
                THEN 'skill'

                WHEN {alias}.concept_kind = 'uri'
                 AND {alias}.concept_uri
                     LIKE '{URI_PREFIXES["occupation"]}%'
                THEN 'occupation'

                WHEN {alias}.concept_kind = 'uri'
                 AND {alias}.concept_uri
                     LIKE '{URI_PREFIXES["skill"]}%'
                THEN 'skill'

                ELSE 'other'
            END
        """

    @staticmethod
    def _obsolete_sql(alias: str = "l") -> str:
        return f"""
            EXISTS (
                SELECT 1
                FROM concept_status AS s
                WHERE s.concept_kind = {alias}.concept_kind
                  AND s.concept_uri = {alias}.concept_uri
                  AND LOWER(TRIM(s.status)) = 'obsolete'
            )
        """

    @staticmethod
    def _kind_basis(
        conn: sqlite3.Connection,
        uri: str,
        kind: str,
    ) -> str:
        explicit = conn.execute(
            """
            SELECT 1
            FROM rdf_triples
            WHERE subject_kind = 'uri'
              AND subject = ?
              AND predicate = ?
              AND object_kind = 'uri'
              AND object = ?
            LIMIT 1
            """,
            (uri, RDF_TYPE, TYPE_URIS[kind]),
        ).fetchone()

        return (
            "rdf_type"
            if explicit is not None
            else "uri_namespace"
        )

    @staticmethod
    def _preferred_label(
        conn: sqlite3.Connection,
        uri: str,
        language_order: tuple[str, ...],
    ) -> tuple[str, str]:
        rows = conn.execute(
            """
            SELECT language, text
            FROM labels
            WHERE concept_kind = 'uri'
              AND concept_uri = ?
              AND label_type = 'prefLabel'
            ORDER BY language, text
            """,
            (uri,),
        ).fetchall()

        if not rows:
            return uri, ""

        for language in language_order:
            for row in rows:
                if row["language"] == language:
                    return row["text"], row["language"]

        return rows[0]["text"], rows[0]["language"]

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        languages: tuple[str, ...] | list[str] | None = ("it", "en"),
        limit: int = 20,
        allow_prefix: bool = True,
        include_obsolete: bool = False,
        include_hidden: bool = False,
    ) -> list[EscoMatch]:
        """
        Search complete labels and words or phrases within labels,
        optionally matching prefixes.

        Multi-word queries must appear as a contiguous sequence.
        This is not fuzzy matching or job-post classification.

        Return at most one match per concept. Set languages to None
        or an empty list to search all available languages.
        """
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(
                "kind must be 'occupation' or 'skill'"
            )

        if limit < 1:
            raise ValueError("limit must be positive")

        normalized = normalize(query.strip())

        if not normalized:
            return []

        language_order = self._languages(languages)

        use_prefix = (
            allow_prefix
            and len(normalized) >= 3
            and normalized[-1].isalnum()
        )

        params: dict[str, str | int] = {
            "query": normalized,
            "allow_prefix": int(use_prefix),
            "limit": limit,
        }

        label_condition = (
            ""
            if include_hidden
            else "AND l.label_type IN ('prefLabel', 'altLabel')"
        )

        language_condition = ""
        language_cases = []

        if language_order:
            placeholders = []

            for position, language in enumerate(language_order):
                key = f"language_{position}"
                params[key] = language
                placeholders.append(f":{key}")
                language_cases.append(
                    f"WHEN :{key} THEN {position}"
                )

            language_condition = (
                "AND l.language IN ("
                + ", ".join(placeholders)
                + ")"
            )

            language_sort = (
                "CASE language "
                + " ".join(language_cases)
                + f" ELSE {len(language_order)} END"
            )

        else:
            language_sort = "language"

        obsolete_condition = (
            ""
            if include_obsolete
            else f"AND NOT ({self._obsolete_sql()})"
        )

        kind_expression = self._kind_sql()

        if kind is None:
            kind_condition = """
                AND entity_kind IN ('occupation', 'skill')
            """
        else:
            kind_condition = "AND entity_kind = :kind"
            params["kind"] = kind

        sql = f"""
            WITH matching_labels AS (
                SELECT
                    l.*,
                    esco_label_rank(
                        l.normalized_text,
                        :query,
                        :allow_prefix
                    ) AS match_rank
                FROM labels AS l
                WHERE l.concept_kind = 'uri'
                  {label_condition}
                  {language_condition}
                  AND esco_label_rank(
                      l.normalized_text,
                      :query,
                      :allow_prefix
                  ) < 4
            ),
            typed_labels AS (
                SELECT
                    l.*,
                    {kind_expression} AS entity_kind,
                    {self._obsolete_sql()} AS obsolete
                FROM matching_labels AS l
                WHERE 1 = 1
                  {obsolete_condition}
            ),
            eligible_labels AS (
                SELECT *
                FROM typed_labels
                WHERE 1 = 1
                  {kind_condition}
            ),
            ranked_labels AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY concept_uri
                        ORDER BY
                            match_rank,
                            CASE label_type
                                WHEN 'prefLabel' THEN 0
                                WHEN 'altLabel' THEN 1
                                ELSE 2
                            END,
                            {language_sort},
                            LENGTH(text),
                            text,
                            language
                    ) AS label_rank
                FROM eligible_labels
            )
            SELECT *
            FROM ranked_labels
            WHERE label_rank = 1
            ORDER BY
                match_rank,
                CASE label_type
                    WHEN 'prefLabel' THEN 0
                    WHEN 'altLabel' THEN 1
                    ELSE 2
                END,
                {language_sort},
                LENGTH(text),
                text,
                concept_uri
            LIMIT :limit
        """

        display_languages = tuple(
            dict.fromkeys((*language_order, "it", "en"))
        )

        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            results: list[EscoMatch] = []

            for row in rows:
                preferred_text, preferred_language = (
                    self._preferred_label(
                        conn,
                        row["concept_uri"],
                        display_languages,
                    )
                )

                results.append(
                    EscoMatch(
                        uri=row["concept_uri"],
                        kind=row["entity_kind"],
                        preferred_label=preferred_text,
                        preferred_language=preferred_language,
                        matched_label=row["text"],
                        matched_language=row["language"],
                        matched_label_type=row["label_type"],
                        match_type=MATCH_TYPES[row["match_rank"]],
                        kind_basis=self._kind_basis(
                            conn,
                            row["concept_uri"],
                            row["entity_kind"],
                        ),
                        obsolete=bool(row["obsolete"]),
                    )
                )

            return results

    def search_occupations(
        self,
        query: str,
        **kwargs,
    ) -> list[EscoMatch]:
        return self.search(
            query,
            kind="occupation",
            **kwargs,
        )

    def search_skills(
        self,
        query: str,
        **kwargs,
    ) -> list[EscoMatch]:
        return self.search(
            query,
            kind="skill",
            **kwargs,
        )

    def labels_for(
        self,
        uri: str,
        languages: tuple[str, ...] | list[str] | None = None,
        include_hidden: bool = False,
    ) -> list[EscoLabel]:
        language_order = self._languages(languages)

        sql = """
            SELECT language, text, label_type
            FROM labels
            WHERE concept_kind = 'uri'
              AND concept_uri = ?
        """
        params: list[str] = [uri]

        if not include_hidden:
            sql += """
                AND label_type IN ('prefLabel', 'altLabel')
            """

        if language_order:
            placeholders = ",".join(
                "?" for _ in language_order
            )
            sql += f" AND language IN ({placeholders})"
            params.extend(language_order)

        sql += """
            ORDER BY
                language,
                CASE label_type
                    WHEN 'prefLabel' THEN 0
                    WHEN 'altLabel' THEN 1
                    ELSE 2
                END,
                text
        """

        with self._connection() as conn:
            return [
                EscoLabel(
                    language=row["language"],
                    text=row["text"],
                    label_type=row["label_type"],
                )
                for row in conn.execute(sql, params)
            ]

    def relations_for(
        self,
        uri: str,
        predicate: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        if limit < 1:
            raise ValueError("limit must be positive")

        sql = """
            SELECT predicate, object_kind, object
            FROM rdf_triples
            WHERE subject_kind = 'uri'
              AND subject = ?
              AND object_kind IN ('uri', 'bnode')
        """
        params: list[str | int] = [uri]

        if predicate is not None:
            sql += " AND predicate = ?"
            params.append(predicate)

        sql += """
            ORDER BY predicate, object
            LIMIT ?
        """
        params.append(limit)

        with self._connection() as conn:
            return [
                dict(row)
                for row in conn.execute(sql, params)
            ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Search occupations and skills "
            "in the local ESCO catalog."
        )
    )

    parser.add_argument("query")

    parser.add_argument(
        "--kind",
        choices=("occupation", "skill"),
    )

    parser.add_argument(
        "--languages",
        default="it,en",
        help="Comma-separated languages, or 'all'.",
    )

    parser.add_argument("--database", default=None)
    parser.add_argument("--limit", type=int, default=10)

    parser.add_argument(
        "--exact",
        action="store_true",
        help=(
            "Disable prefix matching; still match complete words or phrases "
            "within labels."
        ),
    )

    parser.add_argument(
        "--include-obsolete",
        action="store_true",
    )

    parser.add_argument(
        "--include-hidden",
        action="store_true",
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    languages = (
        None
        if args.languages.strip().casefold() == "all"
        else tuple(args.languages.split(","))
    )

    catalog = EscoCatalog(args.database)

    results = catalog.search(
        args.query,
        kind=args.kind,
        languages=languages,
        limit=args.limit,
        allow_prefix=not args.exact,
        include_obsolete=args.include_obsolete,
        include_hidden=args.include_hidden,
    )

    print(
        json.dumps(
            [asdict(result) for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()