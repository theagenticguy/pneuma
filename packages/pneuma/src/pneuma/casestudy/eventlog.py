"""Load a real XES event log into Polars, then persist it to libSQL with WAL.

The log is the ground truth for everything downstream. A process model that was not
derived from what actually happened is a diagram of what somebody believed, and the
gap between those two is the whole reason process mining exists.

Storage is libSQL (the Turso engine) in WAL mode. WAL matters here for a practical
reason rather than a fashionable one: mining reads the log repeatedly while the
interpreter writes run traces to the same database, and WAL lets readers proceed
without blocking the writer. Persistence means a mined model, its verification
verdict, and the runs executed under it all live in one auditable file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import libsql
import polars as pl

# XES stores every attribute as <string key=... value=...>, so the semantic columns
# are conventions rather than schema. These three are the ones that make a log a log.
CASE_ID = "concept:name"
ACTIVITY = "concept:name"
TIMESTAMP = "time:timestamp"


@dataclass(frozen=True)
class LogStats:
    """Headline numbers an executive actually asks for."""

    cases: int
    events: int
    activities: int
    resources: int
    span_days: float
    median_case_hours: float
    p95_case_hours: float


def parse_xes(path: Path) -> pl.DataFrame:
    """Parse an XES file into one row per event.

    Streams with `iterparse` and clears each trace after reading it, so a large log
    does not need to fit in memory as a DOM.
    """
    rows: list[dict[str, object]] = []
    case_id = ""
    case_attributes: dict[str, str] = {}

    for event, element in ET.iterparse(str(path), events=("start", "end")):
        if event == "start" and element.tag == "trace":
            case_id = ""
            case_attributes = {}
            continue

        if event != "end":
            continue

        if element.tag == "event":
            attributes = {
                child.get("key", ""): child.get("value", "")
                for child in element
                if child.get("key")
            }
            rows.append(
                {
                    "case_id": case_id,
                    "activity": attributes.get(ACTIVITY, ""),
                    "timestamp": attributes.get(TIMESTAMP, ""),
                    "resource": attributes.get("org:resource", ""),
                    "group": attributes.get("org:group", ""),
                    "channel": case_attributes.get("channel", ""),
                    "department": case_attributes.get("department", ""),
                }
            )
            element.clear()
        elif element.tag == "trace":
            element.clear()
        elif element.tag in {"string", "date", "int", "float", "boolean"}:
            # A direct child of <trace> is a case attribute; anything deeper belongs
            # to an event and has already been consumed above.
            key = element.get("key", "")
            if key == CASE_ID and not case_id:
                case_id = element.get("value", "")
            elif key in {"channel", "department", "group", "responsible"}:
                case_attributes.setdefault(key, element.get("value", ""))

    frame = pl.DataFrame(rows)
    if frame.is_empty():
        raise ValueError(f"{path} produced no events")

    # XES timestamps carry a UTC offset, and this log spans a DST boundary so the
    # offset itself changes (+02:00 in summer, +01:00 in winter). Polars refuses to
    # guess: parse the offset explicitly, then convert to UTC so durations computed
    # across the boundary are real elapsed time rather than wall-clock arithmetic.
    return (
        frame.with_columns(
            pl.col("timestamp")
            .str.to_datetime(format="%Y-%m-%dT%H:%M:%S%.f%:z", strict=False, time_unit="us")
            .dt.convert_time_zone("UTC")
            .dt.replace_time_zone(None)
            .alias("ts")
        )
        .drop_nulls("ts")
        .sort(["case_id", "ts"])
        .with_columns(pl.col("ts").rank("ordinal").over("case_id").cast(pl.Int32).alias("position"))
    )


def case_durations(events: pl.DataFrame) -> pl.DataFrame:
    """Per-case start, end, duration in hours, and the activity path."""
    return (
        events.group_by("case_id")
        .agg(
            pl.col("ts").min().alias("started"),
            pl.col("ts").max().alias("ended"),
            pl.len().alias("events"),
            pl.col("activity").alias("path"),
            pl.col("channel").first().alias("channel"),
        )
        .with_columns(
            ((pl.col("ended") - pl.col("started")).dt.total_seconds() / 3600).alias("hours")
        )
        .sort("started")
    )


def stats(events: pl.DataFrame) -> LogStats:
    durations = case_durations(events)
    span = (events["ts"].max() - events["ts"].min()).total_seconds() / 86400  # type: ignore[operator]
    return LogStats(
        cases=events["case_id"].n_unique(),
        events=events.height,
        activities=events["activity"].n_unique(),
        resources=events["resource"].n_unique(),
        span_days=round(span, 1),
        median_case_hours=round(float(durations["hours"].median() or 0.0), 1),
        p95_case_hours=round(float(durations["hours"].quantile(0.95) or 0.0), 1),
    )


# ── libSQL persistence ──


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  case_id    TEXT NOT NULL,
  position   INTEGER NOT NULL,
  activity   TEXT NOT NULL,
  ts         TEXT NOT NULL,
  resource   TEXT,
  "group"    TEXT,
  channel    TEXT,
  department TEXT,
  PRIMARY KEY (case_id, position)
);
CREATE INDEX IF NOT EXISTS events_activity ON events(activity);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS mined_models (
  name       TEXT PRIMARY KEY,
  log        TEXT NOT NULL,
  mined_at   TEXT NOT NULL,
  ir_json    TEXT NOT NULL,
  states     INTEGER NOT NULL,
  edges      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
  model      TEXT NOT NULL,
  checker    TEXT NOT NULL,
  verified   INTEGER NOT NULL,
  detail     TEXT,
  checked_at TEXT NOT NULL,
  PRIMARY KEY (model, checker)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  model      TEXT NOT NULL,
  case_id    TEXT,
  final_state TEXT,
  path       TEXT,
  rejections INTEGER NOT NULL DEFAULT 0,
  outcome    TEXT NOT NULL,
  ran_at     TEXT NOT NULL
);
"""


def connect(path: Path) -> libsql.Connection:
    """Open a libSQL database in WAL mode.

    WAL is set on every open because the pragma is per-connection for readers even
    though the mode itself is persisted in the file header.
    """
    connection = libsql.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_schema(connection: libsql.Connection) -> None:
    for statement in filter(str.strip, SCHEMA.split(";")):
        connection.execute(statement)
    connection.commit()


def persist_events(connection: libsql.Connection, events: pl.DataFrame) -> int:
    """Write the log, replacing any prior copy of the same cases."""
    payload = events.select(
        "case_id",
        "position",
        "activity",
        pl.col("ts").dt.strftime("%Y-%m-%dT%H:%M:%S").alias("ts"),
        "resource",
        "group",
        "channel",
        "department",
    ).rows()
    connection.executemany(
        "INSERT OR REPLACE INTO events "
        '(case_id, position, activity, ts, resource, "group", channel, department) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    connection.commit()
    return len(payload)


def read_events(connection: libsql.Connection) -> pl.DataFrame:
    """Read the log back out of libSQL into Polars."""
    cursor = connection.execute(
        'SELECT case_id, position, activity, ts, resource, "group", channel, department '
        "FROM events ORDER BY case_id, position"
    )
    columns = [
        "case_id",
        "position",
        "activity",
        "ts",
        "resource",
        "group",
        "channel",
        "department",
    ]
    return pl.DataFrame(cursor.fetchall(), schema=columns, orient="row").with_columns(
        pl.col("ts").str.to_datetime(strict=False, time_unit="us")
    )
