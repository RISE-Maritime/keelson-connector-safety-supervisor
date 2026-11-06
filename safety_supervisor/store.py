"""The supervisor's own durable store: the state, the processed-request ledger, the policy snapshot
and the assignment projection, in one sqlite database (stdlib, WAL, synchronous=FULL).

THE TRANSACTION IS THE POINT (inv. 4). A transition and its ledger entry commit together, or neither
does; the result is published only after the commit, and marked published after that; a ledger row
is deleted only once published, its request key deleted from storage, and past the anti-replay
retention counted from that delete (crowsnest-dev#279). A crash at any point therefore
leaves either "not processed" (process it fresh) or "processed, maybe unpublished" (republish it) —
never "half".

A store that cannot be trusted — missing, corrupt, a schema it does not know, a state whose hash
does not match — is reported as such and NEVER read as CLEAR: the caller enters RECOVERY_REQUIRED
(I17, inv. 9).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import codec
from .model import Result, State

SCHEMA_VERSION = 1


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(state_json: str) -> str:
    return hashlib.sha256(f"v{SCHEMA_VERSION}:{state_json}".encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit BEGIN/COMMIT below
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ledger (
              request_id TEXT PRIMARY KEY,
              result TEXT NOT NULL,
              published INTEGER NOT NULL DEFAULT 0,
              processed_ms INTEGER NOT NULL
            );
            """
        )
        # The request key's delete (crowsnest-dev#279): NULL until it was deleted from storage. Added in
        # place: SCHEMA_VERSION is inside the state hash, and bumping it would boot RECOVERY_REQUIRED.
        if "deleted_ms" not in {r[1] for r in self.db.execute("PRAGMA table_info(ledger)")}:
            self.db.execute("ALTER TABLE ledger ADD COLUMN deleted_ms INTEGER")

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ state

    def load_state(self) -> tuple[State | None, str]:
        """`(state, "")` when the store can be trusted; `(None, why)` when it cannot."""
        try:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return None, "store failed its integrity check"
            rows = dict(self.db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.DatabaseError as e:
            return None, f"store unreadable: {e}"
        if "state" not in rows:
            return None, "no stored safety state" if not self.existed else "stored safety state missing"
        if rows.get("schema") != str(SCHEMA_VERSION):
            return None, f"unknown store schema {rows.get('schema')!r}"
        if _digest(rows["state"]) != rows.get("state_hash"):
            return None, "stored safety state does not match its hash"
        try:
            return codec.state_from_json(json.loads(rows["state"])), ""
        except (ValueError, KeyError, TypeError) as e:
            return None, f"stored safety state inconsistent: {e}"

    def commit(self, state: State, result: Result | None = None) -> None:
        """The state, and the ledger entry if any, in ONE transaction."""
        state_json = _canonical(codec.state_to_json(state))
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            cur.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('state', ?)", (state_json,))
            cur.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('state_hash', ?)", (_digest(state_json),))
            if result is not None:
                # INSERT, not REPLACE: a request_id answers once, forever (inv. 2, I8).
                cur.execute(
                    "INSERT INTO ledger (request_id, result, published, processed_ms) VALUES (?, ?, 0, ?)",
                    (result.request_id, _canonical(codec.result_to_json(result)), result.processed_at_ms),
                )
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ ledger

    def result_for(self, request_id: str) -> Result | None:
        row = self.db.execute("SELECT result FROM ledger WHERE request_id = ?", (request_id,)).fetchone()
        return codec.result_from_json(json.loads(row[0])) if row else None

    def unpublished(self) -> list[Result]:
        rows = self.db.execute("SELECT result FROM ledger WHERE published = 0 ORDER BY processed_ms").fetchall()
        return [codec.result_from_json(json.loads(r[0])) for r in rows]

    def is_published(self, request_id: str) -> bool:
        row = self.db.execute("SELECT published FROM ledger WHERE request_id = ?", (request_id,)).fetchone()
        return bool(row and row[0])

    def mark_published(self, request_id: str) -> None:
        self.db.execute("UPDATE ledger SET published = 1 WHERE request_id = ?", (request_id,))

    def undeleted(self) -> list[str]:
        """Answered and published, but the request key not (known to be) deleted from storage."""
        rows = self.db.execute("SELECT request_id FROM ledger WHERE published = 1 AND deleted_ms IS NULL "
                               "ORDER BY processed_ms").fetchall()
        return [r[0] for r in rows]

    def mark_deleted(self, request_id: str, now_ms: int) -> None:
        self.db.execute("UPDATE ledger SET deleted_ms = ? WHERE request_id = ? AND published = 1", (now_ms, request_id))

    def stored_again(self, request_id: str) -> None:
        """The request was seen in storage after its delete: not deleted after all; its clock is void."""
        self.db.execute("UPDATE ledger SET deleted_ms = NULL WHERE request_id = ?", (request_id,))

    def deleted_ms(self, request_id: str) -> int | None:
        row = self.db.execute("SELECT deleted_ms FROM ledger WHERE request_id = ?", (request_id,)).fetchone()
        return row[0] if row else None

    def gc(self, now_ms: int, retention_ms: int) -> int:
        """Tombstones go only once published, the request key deleted, AND past the anti-replay
        retention counted FROM THAT DELETE (inv. 4, 5; crowsnest-dev#279)."""
        cur = self.db.execute(
            "DELETE FROM ledger WHERE published = 1 AND deleted_ms IS NOT NULL AND deleted_ms < ?", (now_ms - retention_ms,)
        )
        return cur.rowcount

    # ------------------------------------------------------------------ policy / projection

    def get_json(self, key: str) -> dict | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_json(self, key: str, value: dict) -> None:
        if key in ("state", "state_hash", "schema"):
            raise ValueError("the state is written only through commit()")
        self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, _canonical(value)))
