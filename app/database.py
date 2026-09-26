"""SQLite persistence. The database file lives under DATA_DIR (container volume)."""
from __future__ import annotations

import json
import os
import secrets
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
    -- 合并预览的会话专属待合并快照；确认只凭不可猜测的一次性标识换取。
    -- 不与 sessions 建外键：预览本身不创建会话，会话不存在时也允许生成快照。
    token TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    records_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_merge_previews_session ON merge_previews(session_id);
"""

# 预览快照保留期：24 小时内未确认的待合并快照视为过期。
MERGE_PREVIEW_TTL_HOURS = 24


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
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    # merge_previews 不挂外键（预览不创建会话），显式随会话清理。
    conn.execute("DELETE FROM merge_previews WHERE session_id = ?", (session_id,))
    return cur.rowcount > 0


# ---------- Merge preview snapshots (preview/confirm binding) ----------

def save_merge_preview(
    conn: sqlite3.Connection,
    session_id: str,
    scope: str,
    records: Sequence[Mapping[str, Any]],
    basis: Sequence[Mapping[str, Any]],
) -> str:
    """保存一次合并预览的待合并快照，返回不可猜测的一次性标识。

    同一会话只保留最新一份快照：再次预览使旧标识立即失效，避免堆积与
    「凭旧预览确认」。顺手清掉超过保留期的过期快照。token 由 256 位
    随机数生成（secrets），不可由会话内容或顺序猜测。
    """
    conn.execute(
        "DELETE FROM merge_previews WHERE session_id = ?", (session_id,)
    )
    conn.execute(
        "DELETE FROM merge_previews WHERE created_at < datetime('now', ?)",
        (f"-{MERGE_PREVIEW_TTL_HOURS} hours",),
    )
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO merge_previews(token, session_id, scope, records_json, basis_json) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            token,
            session_id,
            scope,
            json.dumps(list(records), ensure_ascii=False),
            json.dumps(list(basis), ensure_ascii=False),
        ),
    )
    return token


def consume_merge_preview(
    conn: sqlite3.Connection, token: str, session_id: str
) -> dict[str, Any] | None:
    """原子消耗一次合并预览标识并返回其快照。

    用条件 UPDATE 一次性完成「校验未使用 + 标记已用」（同时立即抢下写锁，
    使随后的基线核对与写入在同一写事务内原子完成）。返回值：

    - ``{"status": "ok", ...}``：消耗成功，附带预览时保存的快照；
    - ``{"status": "used"}``：标识存在但已被使用（或刚被并发确认抢走）；
    - ``None``：标识不存在、不属于该会话或已过期。

    快照含 scope / records / basis，均为预览时服务端自行保存的数据，
    确认请求不再接受任何客户端回传的记录或基线。
    """
    row = conn.execute(
        "SELECT session_id, consumed_at, created_at FROM merge_previews WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None or row["session_id"] != session_id:
        return None
    stale = conn.execute(
        "SELECT 1 FROM merge_previews WHERE token = ? "
        "AND created_at < datetime('now', ?)",
        (token, f"-{MERGE_PREVIEW_TTL_HOURS} hours"),
    ).fetchone()
    if stale is not None:
        return None
    cur = conn.execute(
        "UPDATE merge_previews SET consumed_at = datetime('now') "
        "WHERE token = ? AND consumed_at IS NULL",
        (token,),
    )
    if cur.rowcount == 0:
        return {"status": "used"}
    saved = conn.execute(
        "SELECT scope, records_json, basis_json FROM merge_previews WHERE token = ?",
        (token,),
    ).fetchone()
    return {
        "status": "ok",
        "scope": saved["scope"],
        "records": json.loads(saved["records_json"]),
        "basis": json.loads(saved["basis_json"]),
    }


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
