"""
store.py — tabular (SQLite) store for collected TomTom flow readings.

Every collected sample lands in a single relational table so the data can be
queried like a database (SQL) instead of scattered across per-run CSVs. SQLite is
used because it is a zero-dependency, single-file SQL database that ships with
Python — ideal for a store you can also open in any SQL/GUI tool or `sqlite3` CLI.

Table `flow_readings` — one row per (reading, WEH sample point):

    run_id            reading id  (e.g. "peak_20260817_0910")
    fetched_utc       ISO-8601 UTC timestamp of the reading
    fetched_ist       ISO-8601 IST timestamp (convenience)
    segment           peak | avg | offpeak
    label             free-text label
    idx               WEH sample-point index (0=Dahisar … N=Bandra)
    lat, lon          sample-point coordinates
    current_speed_kph, free_speed_kph, tti, confidence, road_closure

Table `intersection_readings` is deliberately separate. It stores fresh provider
observations for the expanded Greater Mumbai junction inventory, keyed by the
inventory's stable string ``point_id`` rather than the WEH ``idx``. Keeping the
tables separate prevents an expanded-area point from being mistaken for a WEH
corridor sample by existing calibration and grouping code.

Inserts are idempotent: UNIQUE(run_id, idx) + INSERT OR IGNORE, so re-running a
backfill or re-importing a CSV never duplicates rows.

CLI:
    python -m src.data.store --info                 # row/segment counts
    python -m src.data.store --backfill             # import collected CSVs
    python -m src.data.store --export flow.csv      # dump the whole table
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data import segments as seg

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "processed" / "traffic.db"
COLLECTED_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    fetched_utc   TEXT NOT NULL,
    fetched_ist   TEXT,
    segment       TEXT,
    label         TEXT,
    idx           INTEGER,
    lat           REAL,
    lon           REAL,
    current_speed_kph REAL,
    free_speed_kph    REAL,
    tti           REAL,
    confidence    REAL,
    road_closure  INTEGER,
    provider      TEXT,
    UNIQUE(run_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_flow_segment ON flow_readings(segment);
CREATE INDEX IF NOT EXISTS ix_flow_fetched ON flow_readings(fetched_utc);
CREATE INDEX IF NOT EXISTS ix_flow_run     ON flow_readings(run_id);

-- Fresh readings for the expanded Greater Mumbai junction inventory. ``scope``
-- is the point's most specific membership: bmc for BMC junctions and mmrda for
-- the MMRDA-only remainder. The MMRDA view is the union of both scopes.
CREATE TABLE IF NOT EXISTS intersection_readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    point_id           TEXT NOT NULL,
    scope              TEXT NOT NULL,
    fetched_utc        TEXT NOT NULL,
    fetched_ist        TEXT,
    segment            TEXT,
    label              TEXT,
    lat                REAL NOT NULL,
    lon                REAL NOT NULL,
    name               TEXT,
    provider           TEXT NOT NULL,
    current_speed_kph  REAL,
    free_speed_kph     REAL,
    tti                REAL,
    confidence         REAL,
    road_closure       INTEGER,
    UNIQUE(run_id, point_id)
);
CREATE INDEX IF NOT EXISTS ix_intersection_point
    ON intersection_readings(point_id);
CREATE INDEX IF NOT EXISTS ix_intersection_scope
    ON intersection_readings(scope);
CREATE INDEX IF NOT EXISTS ix_intersection_fetched
    ON intersection_readings(fetched_utc);
CREATE INDEX IF NOT EXISTS ix_intersection_run
    ON intersection_readings(run_id);

-- Persistent round-robin positions for scheduled collection streams. A cursor
-- lives beside the readings so Docker restarts cannot reset regional coverage
-- to junction zero and repeatedly spend calls on the same first batch.
CREATE TABLE IF NOT EXISTS collection_cursor (
    stream       TEXT PRIMARY KEY,
    next_offset  INTEGER NOT NULL DEFAULT 0,
    updated_utc  TEXT NOT NULL
);

-- Metered API calls reserved per provider per billing month (see src/data/budget.py).
-- Lives here so it shares the readings DB, and therefore the persistent volume:
-- the count has to survive a restart to bound a crash loop.
CREATE TABLE IF NOT EXISTS api_usage (
    provider  TEXT NOT NULL,
    month     TEXT NOT NULL,          -- 'YYYY-MM', UTC
    calls     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, month)
);

-- Provider outages, rate limits and auth failures (see src/data/incidents.py).
-- The trace columns exist so a billing dispute can be evidenced: correlation_id,
-- request_id and server_date are the provider's OWN identifiers for the failed
-- call, which is what their support will ask for.
CREATE TABLE IF NOT EXISTS api_incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,
    occurred_utc    TEXT NOT NULL,
    kind            TEXT NOT NULL,    -- rate_limit | auth | server_error | network | other
    http_status     INTEGER,
    detail          TEXT,
    requests_issued INTEGER,          -- how far the sweep got before aborting
    consecutive     INTEGER,
    endpoint        TEXT,             -- URL path that failed
    correlation_id  TEXT,             -- X-Correlation-ID
    request_id      TEXT,             -- X-Request-Id
    slo             TEXT,             -- x-slo: the service tier the call was rated against
    server_date     TEXT,             -- provider's own Date header
    latency_ms      INTEGER,
    response_body   TEXT,             -- the provider's error payload, truncated
    sample_point    TEXT              -- lat,lon the call was for
);
CREATE INDEX IF NOT EXISTS ix_incident_time ON api_incidents(occurred_utc);

-- Back-off and hard-stop latch per provider. Persistent so a restart cannot
-- forget either: the collector sweeps immediately on startup.
CREATE TABLE IF NOT EXISTS api_hold (
    provider       TEXT PRIMARY KEY,
    consecutive    INTEGER NOT NULL DEFAULT 0,   -- consecutive failed SWEEPS
    hold_until_utc TEXT,                         -- timed back-off
    failed_calls   INTEGER NOT NULL DEFAULT 0,   -- failed CALLS since last success
    latched_utc    TEXT,                         -- hard stop engaged; manual reset only
    latch_reason   TEXT
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so connect() applies these by hand against PRAGMA table_info.
_MIGRATIONS = {
    "flow_readings": {"provider": "TEXT"},
    "api_incidents": {
        "endpoint": "TEXT", "correlation_id": "TEXT", "request_id": "TEXT",
        "slo": "TEXT", "server_date": "TEXT", "latency_ms": "INTEGER",
        "response_body": "TEXT", "sample_point": "TEXT",
    },
    "api_hold": {
        "failed_calls": "INTEGER NOT NULL DEFAULT 0",
        "latched_utc": "TEXT", "latch_reason": "TEXT",
    },
}

_COLUMNS = ["run_id", "fetched_utc", "fetched_ist", "segment", "label", "idx",
            "lat", "lon", "current_speed_kph", "free_speed_kph", "tti",
            "confidence", "road_closure", "provider"]

_INTERSECTION_COLUMNS = [
    "run_id", "point_id", "scope", "fetched_utc", "fetched_ist", "segment",
    "label", "lat", "lon", "name", "provider", "current_speed_kph",
    "free_speed_kph", "tti", "confidence", "road_closure",
]


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB with the schema applied."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    # Lightweight migration: add columns introduced after the DB was first created.
    # A deployed collector keeps its DB on a volume across releases, so a new
    # column has to arrive without a manual step.
    changed = False
    for table, columns in _MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                changed = True
    if changed:
        conn.commit()
    return conn


def _normalize(row: dict, run_id: str) -> tuple:
    """Map a collector/CSV row dict to the ordered DB column tuple."""
    fetched_utc = row.get("fetched_utc") or ""
    segment = row.get("segment") or seg.classify_utc_iso(fetched_utc)
    try:
        fetched_ist = seg.to_ist(datetime.fromisoformat(fetched_utc)).isoformat()
    except (ValueError, TypeError):
        fetched_ist = None
    rc = row.get("roadClosure", row.get("road_closure"))
    rc = int(bool(rc)) if rc is not None and rc != "" else None
    return (
        run_id, fetched_utc, fetched_ist, segment, row.get("label"),
        _int(row.get("idx")), _float(row.get("lat")), _float(row.get("lon")),
        _float(row.get("currentSpeed_kph", row.get("current_speed_kph"))),
        _float(row.get("freeFlowSpeed_kph", row.get("free_speed_kph"))),
        _float(row.get("tti")), _float(row.get("confidence")), rc,
        row.get("provider") or "tomtom",
    )


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def insert_readings(rows: list[dict], run_id: str, conn: sqlite3.Connection | None = None) -> int:
    """Insert reading rows for one run. Returns rows actually inserted."""
    own = conn is None
    conn = conn or connect()
    tuples = [_normalize(r, run_id) for r in rows]
    placeholders = ",".join(["?"] * len(_COLUMNS))
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO flow_readings ({','.join(_COLUMNS)}) VALUES ({placeholders})",
        tuples)
    conn.commit()
    inserted = conn.total_changes - before
    if own:
        conn.close()
    return inserted


def _normalize_intersection(row: dict, run_id: str) -> tuple:
    """Map an expanded-inventory reading to the intersection table columns."""
    fetched_utc = row.get("fetched_utc") or ""
    segment = row.get("segment") or seg.classify_utc_iso(fetched_utc)
    try:
        fetched_ist = seg.to_ist(datetime.fromisoformat(fetched_utc)).isoformat()
    except (ValueError, TypeError):
        fetched_ist = None
    rc = row.get("roadClosure", row.get("road_closure"))
    rc = int(bool(rc)) if rc is not None and rc != "" else None
    return (
        run_id, str(row.get("point_id", row.get("id", ""))), row.get("scope"),
        fetched_utc, fetched_ist, segment, row.get("label"), _float(row.get("lat")),
        _float(row.get("lon")), row.get("name"), row.get("provider") or "tomtom",
        _float(row.get("currentSpeed_kph", row.get("current_speed_kph"))),
        _float(row.get("freeFlowSpeed_kph", row.get("free_speed_kph"))),
        _float(row.get("tti")), _float(row.get("confidence")), rc,
    )


def insert_intersection_readings(
    rows: list[dict], run_id: str, conn: sqlite3.Connection | None = None,
) -> int:
    """Insert fresh expanded-area readings without touching ``flow_readings``.

    Inserts are idempotent on ``(run_id, point_id)``. Returns the number of rows
    actually inserted.
    """
    own = conn is None
    conn = conn or connect()
    tuples = [_normalize_intersection(r, run_id) for r in rows]
    placeholders = ",".join(["?"] * len(_INTERSECTION_COLUMNS))
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO intersection_readings "
        f"({','.join(_INTERSECTION_COLUMNS)}) VALUES ({placeholders})",
        tuples,
    )
    conn.commit()
    inserted = conn.total_changes - before
    if own:
        conn.close()
    return inserted


def load_intersection_readings_df(scope: str | None = None) -> pd.DataFrame:
    """Return expanded-area readings, optionally filtered to a map scope.

    ``scope='bmc'`` returns BMC points only. ``scope='mmrda'`` returns every
    point because MMRDA contains BMC. With no scope, all readings are returned.
    """
    if scope not in (None, "bmc", "mmrda"):
        raise ValueError("scope must be 'bmc', 'mmrda', or None")
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = connect()
    try:
        query = "SELECT * FROM intersection_readings"
        params: tuple = ()
        if scope == "bmc":
            query += " WHERE scope = ?"
            params = ("bmc",)
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def load_latest_intersection_readings(scope: str | None = None) -> pd.DataFrame:
    """Return the newest successful reading for each expanded-area point.

    This is the map export integration point. It reads the authoritative SQLite
    observations directly, so generated coverage JSON never needs synthetic
    placeholder speeds. MMRDA intentionally includes both BMC and MMRDA-only
    point memberships; BMC is the strict subset.
    """
    if scope not in (None, "bmc", "mmrda"):
        raise ValueError("scope must be 'bmc', 'mmrda', or None")
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = connect()
    try:
        where = "WHERE scope = ?" if scope == "bmc" else ""
        params: tuple = ("bmc",) if scope == "bmc" else ()
        return pd.read_sql_query(
            f"""
            SELECT *
            FROM (
                SELECT ir.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY point_id
                           ORDER BY fetched_utc DESC, id DESC
                       ) AS _latest_rank
                FROM intersection_readings AS ir
                {where}
            )
            WHERE _latest_rank = 1
            ORDER BY point_id
            """,
            conn,
            params=params,
        ).drop(columns=["_latest_rank"], errors="ignore")
    finally:
        conn.close()


def load_collection_cursor(stream: str) -> int:
    """Return the next offset for a scheduled stream, defaulting to zero."""
    if not stream.strip():
        raise ValueError("stream must not be empty")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT next_offset FROM collection_cursor WHERE stream = ?", (stream,)
        ).fetchone()
        return max(0, int(row[0])) if row else 0
    finally:
        conn.close()


def save_collection_cursor(stream: str, next_offset: int) -> None:
    """Persist a non-negative round-robin offset atomically."""
    if not stream.strip():
        raise ValueError("stream must not be empty")
    if next_offset < 0:
        raise ValueError("next_offset must be zero or greater")
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO collection_cursor (stream, next_offset, updated_utc)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(stream) DO UPDATE SET
                next_offset = excluded.next_offset,
                updated_utc = excluded.updated_utc
            """,
            (stream, int(next_offset)),
        )
        conn.commit()
    finally:
        conn.close()


def intersection_inventory() -> dict:
    """Small health summary for the scheduled regional collection stream."""
    if not DB_PATH.exists():
        return {"rows": 0, "readings": 0, "points": 0, "last_utc": None}
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT run_id), COUNT(DISTINCT point_id),
                   MAX(fetched_utc)
            FROM intersection_readings
            """
        ).fetchone()
    finally:
        conn.close()
    return {"rows": int(row[0]), "readings": int(row[1]),
            "points": int(row[2]), "last_utc": row[3]}


def load_readings_df() -> pd.DataFrame:
    """Return the whole table as a DataFrame (empty if the DB has no rows)."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = connect()
    try:
        df = pd.read_sql_query("SELECT * FROM flow_readings", conn)
    finally:
        conn.close()
    return df


def has_data() -> bool:
    if not DB_PATH.exists():
        return False
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM flow_readings").fetchone()[0]
    finally:
        conn.close()
    return n > 0


def backfill_csvs(pattern: str = "flow_*.csv") -> int:
    """Import every collected CSV into the DB (idempotent). Returns rows inserted."""
    files = sorted(glob.glob(str(COLLECTED_DIR / pattern)))
    conn = connect()
    total = 0
    try:
        for f in files:
            run_id = Path(f).stem.replace("flow_", "", 1)
            rows = pd.read_csv(f).to_dict(orient="records")
            total += insert_readings(rows, run_id, conn=conn)
    finally:
        conn.close()
    return total


def inventory(df: pd.DataFrame | None = None, recent: int = 25) -> dict:
    """Human-readable summary of everything collected — powers the /data page.

    Derives every view from ONE table read. Returns empty-but-valid structures
    when the DB is missing or empty, which is its state on a fresh deploy before
    the collector's first sweep lands.
    """
    df = load_readings_df() if df is None else df
    empty = {"available": False, "totals": {}, "by_day": [], "recent": [],
             "by_point": [], "by_segment": []}
    if df.empty:
        return empty

    df = df.copy()
    df["ist"] = pd.to_datetime(df["fetched_ist"], errors="coerce", utc=True)
    df["day"] = df["ist"].dt.strftime("%Y-%m-%d")
    valid = df[df["tti"].notna() & (df["tti"] > 0)]

    totals = {
        "rows": int(len(df)),
        "readings": int(df["run_id"].nunique()),
        "points": int(df["idx"].nunique()),
        "days": int(df["day"].nunique()),
        "first_ist": df["fetched_ist"].min(),
        "last_ist": df["fetched_ist"].max(),
        "providers": sorted(p for p in df["provider"].dropna().unique()),
        "mean_tti": round(float(valid["tti"].mean()), 3) if not valid.empty else None,
        "closures": int(df["road_closure"].fillna(0).astype(float).sum()),
    }

    by_day = (df.groupby("day")
                .agg(readings=("run_id", "nunique"), rows=("id", "size"),
                     mean_tti=("tti", "mean"), worst_tti=("tti", "max"),
                     mean_kph=("current_speed_kph", "mean"))
                .round(2).reset_index().sort_values("day", ascending=False))
    # Per-day segment split, so a gap in weekday peak coverage is visible at a glance.
    split = (df.groupby(["day", "segment"])["run_id"].nunique().unstack(fill_value=0))
    for s in ("peak", "avg", "offpeak"):
        by_day[s] = by_day["day"].map(split[s]) if s in split else 0

    by_segment = (df.groupby("segment")
                    .agg(readings=("run_id", "nunique"), rows=("id", "size"),
                         mean_tti=("tti", "mean"), mean_kph=("current_speed_kph", "mean"))
                    .round(2).reset_index())

    recent_runs = (df.groupby("run_id")
                     .agg(when_ist=("fetched_ist", "max"), segment=("segment", "first"),
                          points=("idx", "size"), mean_tti=("tti", "mean"),
                          worst_tti=("tti", "max"), slowest_kph=("current_speed_kph", "min"),
                          provider=("provider", "first"))
                     .round(2).reset_index()
                     .sort_values("when_ist", ascending=False).head(recent))

    by_point = (df.groupby("idx")
                  .agg(obs=("id", "size"), lat=("lat", "first"), lon=("lon", "first"),
                       mean_tti=("tti", "mean"), worst_tti=("tti", "max"),
                       mean_kph=("current_speed_kph", "mean"),
                       free_kph=("free_speed_kph", "mean"))
                  .round(2).reset_index().sort_values("idx"))

    def _rows(frame):
        return frame.where(pd.notna(frame), None).to_dict(orient="records")

    return {"available": True, "totals": totals, "by_day": _rows(by_day),
            "by_segment": _rows(by_segment), "recent": _rows(recent_runs),
            "by_point": _rows(by_point)}


def segment_counts() -> pd.DataFrame:
    df = load_readings_df()
    if df.empty:
        return df
    return (df.groupby("segment")
              .agg(rows=("id", "size"), readings=("run_id", "nunique"),
                   mean_tti=("tti", "mean"))
              .round(3).reset_index())


def main() -> None:
    ap = argparse.ArgumentParser(description="Tabular (SQLite) store for flow readings.")
    ap.add_argument("--info", action="store_true", help="Show row/segment counts.")
    ap.add_argument("--backfill", action="store_true", help="Import collected CSVs into the DB.")
    ap.add_argument("--export", metavar="CSV", help="Export the whole table to a CSV.")
    args = ap.parse_args()

    if args.backfill:
        n = backfill_csvs()
        print(f"[store] Backfilled {n} new rows from CSVs into {DB_PATH.name}")

    if args.export:
        df = load_readings_df()
        df.to_csv(args.export, index=False)
        print(f"[store] Exported {len(df)} rows -> {args.export}")

    if args.info or not (args.backfill or args.export):
        df = load_readings_df()
        print(f"[store] DB: {DB_PATH}")
        print(f"[store] Total rows: {len(df)}   readings: "
              f"{df['run_id'].nunique() if not df.empty else 0}")
        counts = segment_counts()
        if not counts.empty:
            print("\nPer-segment:")
            print(counts.to_string(index=False))


if __name__ == "__main__":
    main()
