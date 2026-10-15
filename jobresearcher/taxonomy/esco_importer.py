from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from rdflib import BNode, Graph, Literal
from rdflib.store import Store


logger = logging.getLogger(__name__)

IMPORTER_VERSION = "1"
BATCH_SIZE = 5000

SKOS = "http://www.w3.org/2004/02/skos/core#"
SKOSXL = "http://www.w3.org/2008/05/skos-xl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
STATUS = "http://purl.org/iso25964/skos-thes#status"


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


class CheckedReader:
    """
    Incremental binary reader.

    Rejects DTD and ENTITY declarations in UTF-8 RDF/XML files.
    Reads the file in chunks instead of loading it all into memory.
    """

    def __init__(self, stream) -> None:
        self.stream = stream
        self.tail = b""

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = 65536

        data = self.stream.read(size)
        probe = (self.tail + data).upper()

        if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
            raise ValueError(
                "DTD and ENTITY declarations are not allowed"
            )

        self.tail = probe[-32:]
        return data

    def close(self) -> None:
        self.stream.close()


class SQLiteWriteStore(Store):
    """
    Import-only store.

    RDFLib sends parsed triples here. RDF queries are handled later
    through SQLite.
    """

    context_aware = False
    formula_aware = False
    graph_aware = False

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.conn = conn
        self.buffer: list[tuple] = []
        self.received = 0
        self.prefixes: dict[str, str] = {}

    def add(self, triple, context, quoted=False) -> None:
        if quoted:
            raise ValueError("Quoted triples are not supported")

        subject, predicate, obj = triple

        subject_kind = "bnode" if isinstance(subject, BNode) else "uri"

        if isinstance(obj, Literal):
            object_kind = "literal"
            language = obj.language or ""
            datatype = str(obj.datatype or "")
        elif isinstance(obj, BNode):
            object_kind = "bnode"
            language = ""
            datatype = ""
        else:
            object_kind = "uri"
            language = ""
            datatype = ""

        self.buffer.append(
            (
                subject_kind,
                str(subject),
                str(predicate),
                object_kind,
                str(obj),
                language,
                datatype,
            )
        )

        self.received += 1

        if len(self.buffer) >= BATCH_SIZE:
            self.flush()

        if self.received % 100000 == 0:
            logger.info(
                "Processed triples: %s",
                f"{self.received:,}",
            )

    def flush(self) -> None:
        if not self.buffer:
            return

        self.conn.executemany(
            """
            INSERT OR IGNORE INTO rdf_triples (
                subject_kind,
                subject,
                predicate,
                object_kind,
                object,
                language,
                datatype
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            self.buffer,
        )
        self.conn.commit()
        self.buffer.clear()

    def bind(self, prefix, namespace, override=True) -> None:
        prefix = str(prefix or "")
        namespace = str(namespace)

        if override or prefix not in self.prefixes:
            self.prefixes[prefix] = namespace

    def namespace(self, prefix):
        return self.prefixes.get(str(prefix or ""))

    def prefix(self, namespace):
        namespace = str(namespace)

        for prefix, value in self.prefixes.items():
            if value == namespace:
                return prefix

        return None

    def namespaces(self):
        yield from self.prefixes.items()

    def close(self, commit_pending_transaction=False) -> None:
        self.flush()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE rdf_triples (
            subject_kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_kind TEXT NOT NULL,
            object TEXT NOT NULL,
            language TEXT NOT NULL,
            datatype TEXT NOT NULL,
            PRIMARY KEY (
                subject_kind,
                subject,
                predicate,
                object_kind,
                object,
                language,
                datatype
            )
        ) WITHOUT ROWID;

        CREATE TABLE labels (
            concept_kind TEXT NOT NULL,
            concept_uri TEXT NOT NULL,
            label_type TEXT NOT NULL,
            language TEXT NOT NULL,
            text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            PRIMARY KEY (
                concept_kind,
                concept_uri,
                label_type,
                language,
                text
            )
        ) WITHOUT ROWID;
        """
    )


def build_indexes_and_labels(conn: sqlite3.Connection) -> None:
    logger.info("Costruzione degli indici RDF")

    conn.executescript(
        """
        CREATE INDEX idx_rdf_predicate
        ON rdf_triples(predicate);

        CREATE INDEX idx_rdf_object
        ON rdf_triples(object_kind, object);

        CREATE INDEX idx_rdf_subject
        ON rdf_triples(subject_kind, subject);
        """
    )

    conn.create_function(
        "search_normalize",
        1,
        normalize,
        deterministic=True,
    )

    for label_type in ("prefLabel", "altLabel", "hiddenLabel"):
        logger.info("Extracting %s", label_type)

        conn.execute(
            """
            INSERT OR IGNORE INTO labels
            SELECT
                subject_kind,
                subject,
                ?,
                language,
                object,
                search_normalize(object)
            FROM rdf_triples
            WHERE predicate = ?
              AND object_kind = 'literal'
              AND LENGTH(TRIM(object)) > 0
            """,
            (label_type, SKOS + label_type),
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO labels
            SELECT
                link.subject_kind,
                link.subject,
                ?,
                literal.language,
                literal.object,
                search_normalize(literal.object)
            FROM rdf_triples AS link
            JOIN rdf_triples AS literal
              ON literal.subject_kind = link.object_kind
             AND literal.subject = link.object
            WHERE link.predicate = ?
              AND link.object_kind IN ('uri', 'bnode')
              AND literal.predicate = ?
              AND literal.object_kind = 'literal'
              AND LENGTH(TRIM(literal.object)) > 0
            """,
            (
                label_type,
                SKOSXL + label_type,
                SKOSXL + "literalForm",
            ),
        )

        conn.commit()

    conn.executescript(
        """
        CREATE INDEX idx_labels_normalized
        ON labels(normalized_text, language);

        CREATE INDEX idx_labels_concept
        ON labels(concept_kind, concept_uri);
        """
    )

    conn.execute(
        """
        CREATE VIEW concept_status AS
        SELECT
            subject_kind AS concept_kind,
            subject AS concept_uri,
            object AS status
        FROM rdf_triples
        WHERE predicate = 'http://purl.org/iso25964/skos-thes#status'
        """
    )

    conn.execute(
        """
        CREATE VIEW active_labels AS
        SELECT labels.*
        FROM labels
        WHERE NOT EXISTS (
            SELECT 1
            FROM concept_status AS s
            WHERE s.concept_kind = labels.concept_kind
              AND s.concept_uri = labels.concept_uri
              AND LOWER(TRIM(s.status)) = 'obsolete'
        )
        """
    )

    conn.commit()


def source_fingerprint(source: Path) -> dict:
    stat = source.stat()

    return {
        "path": str(source.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "importer_version": IMPORTER_VERSION,
    }


def existing_catalog_matches(
    target: Path,
    fingerprint: dict,
) -> bool:
    if not target.is_file():
        return False

    conn = None

    try:
        conn = sqlite3.connect(
            target.resolve().as_uri() + "?mode=ro",
            uri=True,
        )

        rows = dict(
            conn.execute("SELECT key, value FROM metadata")
        )

        return (
            rows.get("status") == "complete"
            and rows.get("source_fingerprint")
            == json.dumps(fingerprint, sort_keys=True)
        )

    except sqlite3.Error:
        return False

    finally:
        if conn is not None:
            conn.close()


def import_esco_if_needed(
    source: str | Path,
    target: str | Path = "data/esco.sqlite3",
    version: str = "1.2.0",
    force: bool = False,
) -> dict:
    source = Path(source).resolve()
    target = Path(target).resolve()

    if not source.is_file():
        raise FileNotFoundError(source)

    if source == target:
        raise ValueError("Source and destination must be different")

    fingerprint = source_fingerprint(source)

    if not force and existing_catalog_matches(target, fingerprint):
        logger.info("ESCO catalog is already up to date")
        return {"status": "unchanged", "database": str(target)}

    target.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix="esco-import-",
        suffix=".sqlite3",
        dir=target.parent,
    )
    os.close(fd)

    temporary = Path(temporary_name)
    conn = None

    try:
        conn = sqlite3.connect(temporary)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA cache_size = -32768")

        create_schema(conn)
        store = SQLiteWriteStore(conn)

        logger.info("Importing RDF/XML: %s", source)

        graph = Graph(
            store=store,
            bind_namespaces="none",
        )

        with source.open("rb") as stream:
            graph.parse(
                source=CheckedReader(stream),
                publicID=source.as_uri(),
                format="xml",
            )

        store.flush()

        if source_fingerprint(source) != fingerprint:
            raise RuntimeError(
                "The source file changed during import"
            )

        build_indexes_and_labels(conn)

        triple_count = conn.execute(
            "SELECT COUNT(*) FROM rdf_triples"
        ).fetchone()[0]

        label_count = conn.execute(
            "SELECT COUNT(*) FROM labels"
        ).fetchone()[0]

        if triple_count == 0 or label_count == 0:
            raise RuntimeError(
                "Import produced no triples or labels; "
                "catalog was not published"
            )

        metadata = {
            "status": "complete",
            "version": version,
            "source_fingerprint": json.dumps(
                fingerprint,
                sort_keys=True,
            ),
            "triple_count": str(triple_count),
            "label_count": str(label_count),
        }

        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            metadata.items(),
        )
        conn.commit()

        conn.close()
        conn = None

        os.replace(temporary, target)

        result = {
            "status": "imported",
            "database": str(target),
            "triples": triple_count,
            "labels": label_count,
        }

        logger.info("ESCO catalog is ready: %s", result)
        return result

    except BaseException:
        logger.exception("ESCO import failed")
        raise

    finally:
        if conn is not None:
            conn.close()

        if temporary.exists():
            temporary.unlink()

        journal = Path(str(temporary) + "-journal")
        if journal.exists():
            journal.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import ESCO RDF/XML into a local SQLite catalog."
    )

    parser.add_argument(
        "--source",
        default="esco-v1.2.0.rdf",
    )
    parser.add_argument(
        "--database",
        default="data/esco.sqlite3",
    )
    parser.add_argument(
        "--version",
        default="1.2.0",
    )
    parser.add_argument(
        "--force",
        action="store_true",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import_esco_if_needed(
        source=args.source,
        target=args.database,
        version=args.version,
        force=args.force,
    )


if __name__ == "__main__":
    main()