"""SQLite persistence. The database file lives under DATA_DIR (container volume)."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

DEFAULT_DATA_DIR = os.environ.get("CALIB_DATA_DIR", "/data")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'judge',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    contestant TEXT NOT NULL,
    judge TEXT NOT NULL,
    item TEXT NOT NULL,
    score REAL NOT NULL,
    row_order INTEGER NOT NULL,
    UNIQUE (session_id, contestant, judge, item),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decisions (
    score_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    use_raw INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (score_id) REFERENCES scores(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS batch_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    judge TEXT,
    item TEXT,
    raw_min REAL,
    raw_max REAL,
    cal_min REAL,
    cal_max REAL,
    use_raw INTEGER NOT NULL DEFAULT 0,
    reason_template TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_batch_rules_session ON batch_rules(session_id, position);
CREATE TABLE IF NOT EXISTS merge_previews (
    token TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'judge',
    records TEXT NOT NULL,
    basis TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_merge_previews_session ON merge_previews(session_id);
"""


class NotFoundError(LookupError):
    pass


def db_path() -> str:
    return os.path.join(DEFAULT_DATA_DIR, "calibration.db")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发确认同一合并标识时，第二个写事务等待而非立即报 SQLITE_BUSY，
    # 随后在原子消耗（UPDATE … WHERE consumed = 0）上得到 rowcount 0。
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def ensure_session(conn: sqlite3.Connection, session_id: str, scope: str = "judge") -> None:
    conn.execute(
        "INSERT INTO sessions(id, scope) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET updated_at = datetime('now')",
        (session_id, scope),
    )


def set_scope(conn: sqlite3.Connection, session_id: str, scope: str) -> None:
    cur = conn.execute("UPDATE sessions SET scope = ?, updated_at = datetime('now') WHERE id = ?", (scope, session_id))
    if cur.rowcount == 0:
        raise NotFoundError(f"会话不存在: {session_id}")


def get_scope(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT scope FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row["scope"] if row else None


def replace_scores(conn: sqlite3.Connection, session_id: str, records: Sequence[dict[str, Any]]) -> int:
    """Wipe the session's scores/decisions/rules and insert new records."""
    conn.execute("DELETE FROM decisions WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM batch_rules WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM scores WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT INTO scores(session_id, contestant, judge, item, score, row_order) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        [
            (session_id, r["contestant"], r["judge"], r["item"], float(r["score"]), idx)
            for idx, r in enumerate(records)
        ],
    )
    conn.execute("UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,))
    return len(records)


def merge_scores(
    conn: sqlite3.Connection,
    session_id: str,
    plan_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Apply a merge-import plan inside the caller's open transaction.

    Plan rows use the status strings from app.merge:
    - "changed":  原地 UPDATE 分数并删除该条旧裁决（行 id 保持不变）；
    - "removed":  先删裁决再删评分（外键级联同样会删，显式删除以便计数）；
    - "added":    按新文件顺序追加到 row_order 末尾；
    - "unchanged": 完全不触碰。

    批处理规则（batch_rules）与未变记录上的裁决均不修改。
    返回各状态条数与实际清除的裁决数；任何一步失败由调用方的事务整体回滚。
    """
    counts = {"changed": 0, "added": 0, "removed": 0, "unchanged": 0}
    decisions_cleared = 0
    removed_ids: list[int] = []
    added: list[Mapping[str, Any]] = []

    for row in plan_rows:
        status = row["status"]
        counts[status] += 1
        score_id = row.get("score_id")
        if status == "changed":
            cur = conn.execute(
                "DELETE FROM decisions WHERE score_id = ? AND session_id = ?",
                (score_id, session_id),
            )
            decisions_cleared += cur.rowcount
            conn.execute(
                "UPDATE scores SET score = ? WHERE id = ? AND session_id = ?",
                (float(row["new_score"]), score_id, session_id),
            )
        elif status == "removed":
            cur = conn.execute(
                "DELETE FROM decisions WHERE score_id = ? AND session_id = ?",
                (score_id, session_id),
            )
            decisions_cleared += cur.rowcount
            removed_ids.append(score_id)
        elif status == "added":
            added.append(row)
        # "unchanged": 不动。

    if removed_ids:
        conn.executemany(
            "DELETE FROM scores WHERE id = ? AND session_id = ?",
            [(sid, session_id) for sid in removed_ids],
        )

    if added:
        max_row = conn.execute(
            "SELECT COALESCE(MAX(row_order), -1) AS m FROM scores WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]
        added_sorted = sorted(
            added, key=lambda r: r["incoming_order"] if r["incoming_order"] is not None else 0
        )
        conn.executemany(
            "INSERT INTO scores(session_id, contestant, judge, item, score, row_order) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    r["contestant"],
                    r["judge"],
                    r["item"],
                    float(r["new_score"]),
                    max_row + 1 + idx,
                )
                for idx, r in enumerate(added_sorted)
            ],
        )

    conn.execute(
        "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,)
    )
    return {"counts": counts, "decisions_cleared": decisions_cleared}


# ---------- Merge preview snapshots (preview/confirm binding) ----------


def save_merge_preview(
    conn: sqlite3.Connection,
    session_id: str,
    token: str,
    records: Sequence[Mapping[str, Any]],
    basis: Sequence[Mapping[str, Any]],
    scope: str,
) -> None:
    """保存会话专属的待合并快照（预览时不写评分/裁决，也不创建会话）。

    快照含预览时刻解析出的待合并记录、预览基线与校准范围，确认时仅凭
    不可猜测的 ``token`` 取回——确认请求不携带任何记录，客户端无法注入
    未预览的数据。同一会话只保留最新一次预览：旧快照（含已消耗的）随
    新预览一并作废。
    """
    conn.execute("DELETE FROM merge_previews WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO merge_previews(token, session_id, scope, records, basis) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            token,
            session_id,
            scope,
            json.dumps(list(records), ensure_ascii=False),
            json.dumps(list(basis), ensure_ascii=False),
        ),
    )


def claim_merge_preview(
    conn: sqlite3.Connection, session_id: str, token: str
) -> dict[str, Any] | None:
    """在调用方的事务内原子消耗并取回待合并快照。

    返回 ``None`` 表示标识无效（不存在、不属于该会话或已被更新的预览
    作废）；返回 ``{"already_used": True}`` 表示标识已被消耗（一次性，
    防止重复确认）；成功时返回 ``{"records", "basis", "scope"}`` 并把
    标识标记为已消耗。``UPDATE … WHERE consumed = 0`` 使消耗是原子的：
    并发确认同一标识时只有一个事务得到 rowcount 1。若后续基线核对或
    合并写入失败，调用方事务整体回滚，消耗一并撤销。
    """
    cur = conn.execute(
        "UPDATE merge_previews SET consumed = 1 "
        "WHERE token = ? AND session_id = ? AND consumed = 0",
        (token, session_id),
    )
    if cur.rowcount == 1:
        row = conn.execute(
            "SELECT scope, records, basis FROM merge_previews WHERE token = ?",
            (token,),
        ).fetchone()
        return {
            "scope": row["scope"],
            "records": json.loads(row["records"]),
            "basis": json.loads(row["basis"]),
        }
    row = conn.execute(
        "SELECT consumed FROM merge_previews WHERE token = ? AND session_id = ?",
        (token, session_id),
    ).fetchone()
    if row is None:
        return None
    return {"already_used": True}


def delete_merge_previews(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute(
        "DELETE FROM merge_previews WHERE session_id = ?", (session_id,)
    )
    return cur.rowcount


def load_scores(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, contestant, judge, item, score FROM scores "
        "WHERE session_id = ? ORDER BY row_order, id",
        (session_id,),
    ).fetchall()
    return [
        {"id": r["id"], "contestant": r["contestant"], "judge": r["judge"], "item": r["item"], "score": r["score"]}
        for r in rows
    ]


def load_decisions(conn: sqlite3.Connection, session_id: str) -> dict[int, tuple[bool, str]]:
    rows = conn.execute(
        "SELECT score_id, use_raw, reason FROM decisions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["score_id"]: (bool(r["use_raw"]), r["reason"]) for r in rows}


def upsert_decision(conn: sqlite3.Connection, session_id: str, score_id: int, use_raw: bool, reason: str) -> None:
    score = conn.execute(
        "SELECT 1 FROM scores WHERE id = ? AND session_id = ?", (score_id, session_id)
    ).fetchone()
    if not score:
        raise NotFoundError(f"评分记录不存在: id={score_id}")
    conn.execute(
        "INSERT INTO decisions(score_id, session_id, use_raw, reason) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(score_id) DO UPDATE SET use_raw=excluded.use_raw, "
        "reason=excluded.reason, updated_at=datetime('now')",
        (score_id, session_id, 1 if use_raw else 0, reason),
    )


def reset_decisions(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute("DELETE FROM decisions WHERE session_id = ?", (session_id,))
    return cur.rowcount


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    # merge_previews 无外键（预览不创建会话），需随会话显式清理。
    conn.execute("DELETE FROM merge_previews WHERE session_id = ?", (session_id,))
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cur.rowcount > 0


def session_has_data(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM scores WHERE session_id = ?", (session_id,)).fetchone()
    return row["n"] > 0


# ---------- Batch adjudication rules ----------

RULE_FIELDS = (
    "name", "judge", "item", "raw_min", "raw_max",
    "cal_min", "cal_max", "use_raw", "reason_template", "priority",
)


def _rule_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "position": r["position"],
        "name": r["name"],
        "judge": r["judge"],
        "item": r["item"],
        "raw_min": r["raw_min"],
        "raw_max": r["raw_max"],
        "cal_min": r["cal_min"],
        "cal_max": r["cal_max"],
        "use_raw": bool(r["use_raw"]),
        "reason_template": r["reason_template"],
        "priority": r["priority"],
    }


def replace_rules(conn: sqlite3.Connection, session_id: str, rules: Sequence[Mapping[str, Any]]) -> int:
    """Replace the whole rule set of a session, preserving list order."""
    conn.execute("DELETE FROM batch_rules WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT INTO batch_rules("
        "session_id, position, name, judge, item, raw_min, raw_max, "
        "cal_min, cal_max, use_raw, reason_template, priority) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                session_id,
                idx,
                r.get("name", ""),
                r.get("judge"),
                r.get("item"),
                r.get("raw_min"),
                r.get("raw_max"),
                r.get("cal_min"),
                r.get("cal_max"),
                1 if r.get("use_raw") else 0,
                r.get("reason_template", ""),
                int(r["priority"]),
            )
            for idx, r in enumerate(rules)
        ],
    )
    conn.execute("UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,))
    return len(rules)


def load_rules(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, position, name, judge, item, raw_min, raw_max, cal_min, cal_max, "
        "use_raw, reason_template, priority FROM batch_rules "
        "WHERE session_id = ? ORDER BY position, id",
        (session_id,),
    ).fetchall()
    return [_rule_row_to_dict(r) for r in rows]


def delete_rules(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute("DELETE FROM batch_rules WHERE session_id = ?", (session_id,))
    return cur.rowcount


def apply_batch_decisions(
    conn: sqlite3.Connection,
    session_id: str,
    assignments: Sequence[Mapping[str, Any]],
) -> int:
    """Upsert decisions for every rule-matched score in the open transaction.

    Unmatched scores keep their existing decisions. Returns the number of
    decision rows written. Caller is responsible for committing together with
    rule persistence so the whole batch is atomic.
    """
    rows = [
        (
            1 if a["use_raw"] else 0,
            a["reason"],
            a["score_id"],
            session_id,
        )
        for a in assignments
        if a.get("matched")
    ]
    conn.executemany(
        "INSERT INTO decisions(score_id, session_id, use_raw, reason) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(score_id) DO UPDATE SET use_raw=excluded.use_raw, "
        "reason=excluded.reason, updated_at=datetime('now')",
        [(score_id, sid, use_raw, reason) for use_raw, reason, score_id, sid in rows],
    )
    return len(rows)
