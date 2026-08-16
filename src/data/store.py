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
"""

_COLUMNS = ["run_id", "fetched_utc", "fetched_ist", "segment", "label", "idx",
            "lat", "lon", "current_speed_kph", "free_speed_kph", "tti",
            "confidence", "road_closure", "provider"]


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB with the schema applied."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    # Lightweight migration: add columns introduced after the DB was first created.
    have = {r[1] for r in conn.execute("PRAGMA table_info(flow_readings)")}
    if "provider" not in have:
        conn.execute("ALTER TABLE flow_readings ADD COLUMN provider TEXT")
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
