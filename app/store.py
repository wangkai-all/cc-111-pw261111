"""SQLite persistence: immutable rule versions, run snapshots, audit trail."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "linker.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    side_signature_json TEXT NOT NULL,
    note TEXT,
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES versions(id)
);
CREATE TABLE IF NOT EXISTS version_pairs (
    version_id TEXT NOT NULL,
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    key TEXT NOT NULL,
    mode TEXT,
    PRIMARY KEY (version_id, a, b),
    FOREIGN KEY (version_id) REFERENCES versions(id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""

_lock = threading.Lock()


def configure(path: Path | str) -> None:
    """Point the store at a specific database (tests, deployments)."""

    global DB_PATH
    DB_PATH = Path(path)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: Path | None = None) -> None:
    with _lock, _connect(path) as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]
        if count == 0:
            conn.execute(
                "INSERT INTO audit (at, action, actor, detail_json) VALUES (?,?,?,?)",
                (_now(), "init", "system", json.dumps({"message": "database created"},
                                                     ensure_ascii=False)),
            )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_version(actor: str, label: str, rules: dict[str, str],
                   config: dict[str, Any], decisions: dict[str, Any],
                   side_signatures: dict[str, Any], note: str | None = None,
                   version_id: str | None = None,
                   fingerprint: str | None = None,
                   path: Path | None = None) -> dict[str, Any]:
    version_id = version_id or "v" + uuid.uuid4().hex[:10]
    label = (label or "").strip() or version_id
    if fingerprint is None:
        canonical = json.dumps(rules, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with _lock, _connect(path) as conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM versions"
                           ).fetchone()["s"]
        conn.execute(
            "INSERT INTO versions (id, label, created_at, rules_json, "
            "rules_fingerprint, config_json, decisions_json, side_signature_json, "
            "note, seq) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version_id, label, _now(),
             json.dumps(rules, ensure_ascii=False, sort_keys=True),
             fingerprint,
             json.dumps(config, ensure_ascii=False, sort_keys=True),
             json.dumps(decisions, ensure_ascii=False, sort_keys=True),
             json.dumps(side_signatures, ensure_ascii=False, sort_keys=True),
             note, seq),
        )
        conn.execute(
            "INSERT INTO audit (at, action, actor, detail_json) VALUES (?,?,?,?)",
            (_now(), "version.create", actor,
             json.dumps({"version_id": version_id, "label": label,
                         "note": note}, ensure_ascii=False)),
        )
    return get_version(version_id, path)  # type: ignore[arg-type]


def get_version(version_id: str, path: Path | None = None) -> dict[str, Any] | None:
    with _lock, _connect(path) as conn:
        row = conn.execute("SELECT * FROM versions WHERE id = ?",
                           (version_id,)).fetchone()
    return _version_from_row(row) if row else None


def list_versions(path: Path | None = None) -> list[dict[str, Any]]:
    with _lock, _connect(path) as conn:
        rows = conn.execute("SELECT * FROM versions ORDER BY seq DESC").fetchall()
        run_rows = conn.execute(
            "SELECT version_id, COUNT(*) AS n FROM runs GROUP BY version_id"
        ).fetchall()
    counts = {row["version_id"]: row["n"] for row in run_rows}
    out = []
    for row in rows:
        item = _version_from_row(row)
        item["run_count"] = counts.get(item["id"], 0)
        out.append(item)
    return out


def _version_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "rules": json.loads(row["rules_json"]),
        "rules_fingerprint": row["rules_fingerprint"],
        "config": json.loads(row["config_json"]),
        "decisions": json.loads(row["decisions_json"]),
        "data_signature": json.loads(row["side_signature_json"]),
        "note": row["note"],
        "seq": row["seq"],
    }


def save_run(actor: str, version_id: str, analysis: dict[str, Any],
             path: Path | None = None) -> str:
    run_id = "r" + uuid.uuid4().hex[:10]
    summary = analysis.get("summary", {})
    with _lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO runs (id, version_id, created_at, analysis_json, "
            "summary_json) VALUES (?,?,?,?,?)",
            (run_id, version_id, _now(),
             json.dumps(analysis, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO audit (at, action, actor, detail_json) VALUES (?,?,?,?)",
            (_now(), "run.save", actor,
             json.dumps({"run_id": run_id, "version_id": version_id,
                         "summary": summary}, ensure_ascii=False)),
        )
    return run_id


def get_run(run_id: str, path: Path | None = None) -> dict[str, Any] | None:
    with _lock, _connect(path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "version_id": row["version_id"],
        "created_at": row["created_at"],
        "analysis": json.loads(row["analysis_json"]),
        "summary": json.loads(row["summary_json"]),
    }


def list_runs(version_id: str | None = None,
              path: Path | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, version_id, created_at, summary_json FROM runs"
    args: tuple[Any, ...] = ()
    if version_id:
        sql += " WHERE version_id = ?"
        args = (version_id,)
    sql += " ORDER BY created_at DESC, rowid DESC"
    with _lock, _connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {"id": row["id"], "version_id": row["version_id"],
         "created_at": row["created_at"],
         "summary": json.loads(row["summary_json"])}
        for row in rows
    ]


def save_version_pairs(version_id: str, pairs: list[dict[str, Any]],
                       path: Path | None = None) -> None:
    with _lock, _connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO version_pairs (version_id, a, b, key, mode) "
            "VALUES (?,?,?,?,?)",
            [(version_id, int(p["a"]), int(p["b"]), str(p.get("key", "")),
              p.get("mode")) for p in pairs],
        )


def get_version_pairs(version_id: str,
                      path: Path | None = None) -> list[dict[str, Any]]:
    with _lock, _connect(path) as conn:
        rows = conn.execute(
            "SELECT a, b, key, mode FROM version_pairs WHERE version_id = ? "
            "ORDER BY a, b", (version_id,)
        ).fetchall()
    return [{"a": row["a"], "b": row["b"], "key": row["key"],
             "mode": row["mode"]} for row in rows]


def delete_version(version_id: str, actor: str,
                   path: Path | None = None) -> bool:
    with _lock, _connect(path) as conn:
        row = conn.execute("SELECT id FROM versions WHERE id = ?",
                           (version_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM version_pairs WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM runs WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM versions WHERE id = ?", (version_id,))
        conn.execute(
            "INSERT INTO audit (at, action, actor, detail_json) VALUES (?,?,?,?)",
            (_now(), "version.delete", actor,
             json.dumps({"version_id": version_id}, ensure_ascii=False)),
        )
    return True


def record_audit(actor: str, action: str, detail: dict[str, Any],
                 path: Path | None = None) -> None:
    with _lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO audit (at, action, actor, detail_json) VALUES (?,?,?,?)",
            (_now(), action, actor, json.dumps(detail, ensure_ascii=False)),
        )


def list_audit(limit: int = 100, path: Path | None = None) -> list[dict[str, Any]]:
    with _lock, _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"at": row["at"], "action": row["action"], "actor": row["actor"],
         "detail": json.loads(row["detail_json"])}
        for row in rows
    ]
