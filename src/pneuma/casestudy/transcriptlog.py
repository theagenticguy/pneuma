"""Turn an AI agent's own tool-use transcripts into the event log the miner consumes.

The building-permit log is a clean artifact: a human business process, curated for
publication, 27 activities, trace lengths that cluster. It is a good first fixture and
a bad only fixture, because a pipeline validated on one shape of log has only been
shown to work on that shape.

This adapter supplies a structurally different second fixture from the most
self-referential source available: Claude Code JSONL transcripts, read through the
`claude-sql` CLI's DuckDB views. `tool_calls` is already an event-log triple —
`session_id` is the case, `tool_name` the activity, `ts` the timestamp — so the
interesting work is not extraction but the three ways this log differs from a curated
one, each of which the miner would otherwise meet silently:

Scale. The full corpus is 336k events over 5,258 cases against the permit log's 8,577
over 1,434. Sampling is therefore a first-class parameter rather than something a
caller does beforehand, and every sampling decision is recorded on `TranscriptStats`.

Alphabet. Raw `tool_name` gives 262 activities, and MCP names share long prefixes:
`mcp__gateway__amazon-sharepoint-mcp_sharepoint_read_file` and
`..._sharepoint_resolve_url` differ only past character 48. `miner._identifier`
truncates to 40, so raw names *collide*: 5 collision groups covering 12 names across
the corpus. This is not a hypothetical. `miner.mine(events, granularity="tool")` on the
fleet corpus raises `ValidationError: duplicate state names` from the `Process`
validator, so `granularity="tool"` is currently unusable on this log. That the IR
rejects it rather than silently merging two activities is the validator doing its job,
and it is why *family* is the default. `collisions()` lets a caller check any
granularity against the miner's real truncation ahead of time instead of assuming.

Shape. Trace lengths run from 1 to 9,038 with a median of 15, and one activity (`Bash`)
is 63% of all events in the fleet corpus. A one-event case cannot contribute a
directly-follows edge, so `min_trace_length` exists — but it defaults to 1, meaning no
filtering, because a default that quietly discarded a fifth of the log would make this
fixture look better behaved than it is.

Every bound here is reported. `TranscriptStats` carries the counts before and after
each filter and `dropped_*` fields naming what went, so a caller can always answer
"what did the adapter not show me". That is the whole point: an adapter that made the
log look cleaner than it is would be a worse fixture than no second fixture at all.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import polars as pl

from .miner import _identifier

CLAUDE_SQL = "claude-sql"

# Two globs worth naming: the bonk fleet's own agent runs, and every transcript on the
# box. The fleet corpus is the honest comparator for the permit log (similar event
# count); the full corpus is the scale test.
FLEET_GLOB = "/home/lalsaado/bonk-fs/bonk-config-dirs/.claude/projects/**/*.jsonl"
FULL_GLOB = "/home/lalsaado/.claude/projects/**/*.jsonl"

Granularity = Literal["tool", "family", "server"]
Sampling = Literal["longest", "random"]

# `position` is derived from timestamp order, and a handful of calls in one assistant
# message share a timestamp to the millisecond (9 of 9,081 in the fleet corpus, up to
# 4 at once). Ordering by tool_use_id as a tiebreak makes the log deterministic rather
# than dependent on DuckDB's row order.
_QUERY = """
SELECT c.session_id,
       c.ts,
       c.tool_name,
       c.tool_use_id,
       coalesce(m.model, '')      AS model,
       coalesce(s.cwd, '')        AS cwd,
       coalesce(s.git_branch, '') AS git_branch
FROM tool_calls c
LEFT JOIN messages m ON m.uuid = c.message_uuid
LEFT JOIN sessions s ON s.session_id = c.session_id
ORDER BY c.session_id, c.ts, c.tool_use_id
"""


@dataclass(frozen=True)
class TranscriptStats:
    """What the adapter read, and everything it refused to pass on.

    The `dropped_*` fields are the reason this dataclass exists. A caller that only
    sees the surviving log cannot tell a naturally clean process from an aggressively
    filtered one, and those two claims are not the same claim.
    """

    raw_events: int
    raw_cases: int
    raw_activities: int
    events: int
    cases: int
    activities: int
    granularity: str
    min_trace_length: int
    min_activity_cases: int
    max_cases: int | None
    sampling: str
    dropped_short_cases: int
    dropped_short_events: int
    dropped_rare_activities: int
    dropped_rare_events: int
    dropped_sampled_cases: int
    dropped_sampled_events: int
    identifier_collisions: dict[str, list[str]]

    @property
    def dropped_events(self) -> int:
        return self.raw_events - self.events

    @property
    def dropped_share(self) -> float:
        if not self.raw_events:
            return 0.0
        return round(self.dropped_events / self.raw_events, 4)

    def report(self) -> str:
        """One block a caller can print instead of reconstructing the accounting."""
        lines = [
            f"raw:   {self.raw_events} events / {self.raw_cases} cases "
            f"/ {self.raw_activities} activities",
            f"kept:  {self.events} events / {self.cases} cases "
            f"/ {self.activities} activities "
            f"({100 * self.dropped_share:.1f}% of events dropped)",
            f"knobs: granularity={self.granularity} min_trace_length={self.min_trace_length} "
            f"min_activity_cases={self.min_activity_cases} max_cases={self.max_cases}"
            + (f" sampling={self.sampling}" if self.max_cases else ""),
        ]
        if self.dropped_short_cases:
            lines.append(
                f"  short traces:     -{self.dropped_short_cases} cases "
                f"/ -{self.dropped_short_events} events"
            )
        if self.dropped_rare_activities:
            lines.append(
                f"  rare activities:  -{self.dropped_rare_activities} activities "
                f"/ -{self.dropped_rare_events} events"
            )
        if self.dropped_sampled_cases:
            lines.append(
                f"  case sampling:    -{self.dropped_sampled_cases} cases "
                f"/ -{self.dropped_sampled_events} events ({self.sampling})"
            )
            if self.sampling == "longest":
                lines.append(
                    "    NOTE: 'longest' is a biased sample. On the full corpus it took "
                    "conformance from 0.81 to 0.06, because the longest sessions are the "
                    "least typical ones. Use sampling='random' for an unbiased subsample."
                )
        if self.identifier_collisions:
            lines.append(
                f"  WARNING: {len(self.identifier_collisions)} activity name groups collide "
                "under miner._identifier's 40-char truncation. miner.mine will raise "
                "ValidationError('duplicate state names') on this log:"
            )
            lines.extend(
                f"    {name} <- {sorted(members)}"
                for name, members in sorted(self.identifier_collisions.items())
            )
        return "\n".join(lines)


def available() -> bool:
    """Is the `claude-sql` CLI on PATH? Tests that need live data skip on False."""
    return shutil.which(CLAUDE_SQL) is not None


def fetch(glob: str, *, timeout: int = 600) -> list[dict[str, object]]:
    """Run the tool-call query through `claude-sql` and return raw rows.

    Flags go after the subcommand: `claude-sql query --format json SQL`. The reverse
    order fails, because cyclopts binds `--format` before it has a subcommand to bind
    it to, and the error it gives does not say so.
    """
    result = subprocess.run(
        [CLAUDE_SQL, "query", "--format", "json", "--glob", glob, _QUERY],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude-sql exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError(f"expected a JSON array of rows, got {type(rows).__name__}")
    return rows


def tool_family(tool_name: str) -> str:
    """Collapse an MCP tool name to `mcp:<server>:<sub>`, leaving built-ins alone.

    Three naming conventions coexist in the corpus, and none of them is documented:

        Bash                                                    a built-in
        mcp__bonk__list_runs                                    server__leaf
        mcp__gateway__slack-mcp_get_thread                      gateway flattens a
        mcp__plugin_personal-plugins_aws-outlook-mcp__email_read   sub-server into
                                                                the leaf with '_'

    An aggregator (`gateway`, or a plugin host) multiplexes many independent servers,
    so collapsing to just the aggregator would merge Slack, Outlook and SharePoint into
    one activity and destroy the structure worth mining. Keeping one level below the
    aggregator gives 102 activities corpus-wide instead of 262, with no truncation
    collisions.
    """
    if not tool_name.startswith("mcp__"):
        return tool_name
    rest = tool_name[5:]
    if "__" not in rest:
        return f"mcp:{rest.split('_')[0]}"
    server, leaf = rest.split("__", 1)
    if server == "gateway" or server.startswith("plugin"):
        return f"mcp:{server}:{leaf.split('_')[0]}"
    return f"mcp:{server}"


def tool_server(tool_name: str) -> str:
    """Collapse to the transport only: 74 activities corpus-wide.

    Offered for comparison, not as a default. It merges every gateway-hosted server
    into one activity, which is why `family` is the default instead.
    """
    if not tool_name.startswith("mcp__"):
        return tool_name
    rest = tool_name[5:]
    return "mcp:" + (rest.split("__", 1)[0] if "__" in rest else rest.split("_")[0])


_GRANULARITY: dict[str, object] = {
    "tool": lambda name: name,
    "family": tool_family,
    "server": tool_server,
}


def collisions(activities: list[str]) -> dict[str, list[str]]:
    """Group activity names that `miner._identifier` maps to the same identifier.

    Checked rather than assumed. The permit and road-fines logs have short activity
    names and no collisions; MCP names share prefixes far past 40 characters and do
    collide, at which point `Process` rejects the mined model outright. Call this
    before mining to get the diagnosis in terms of tool names rather than identifiers.
    """
    grouped: dict[str, list[str]] = {}
    for activity in sorted(set(activities)):
        grouped.setdefault(_identifier(activity), []).append(activity)
    return {name: members for name, members in grouped.items() if len(members) > 1}


def to_events(
    rows: list[dict[str, object]],
    *,
    granularity: Granularity = "family",
    min_trace_length: int = 1,
    min_activity_cases: int = 1,
    max_cases: int | None = None,
    sampling: Sampling = "random",
    seed: int = 0,
) -> tuple[pl.DataFrame, TranscriptStats]:
    """Shape raw `claude-sql` rows into `eventlog.parse_xes`'s output contract.

    The returned frame carries the same nine columns `parse_xes` produces, with the
    same dtypes, so `miner.mine`, `rules.derive_precedences`, `pipeline` and
    `eventlog.persist_events` all work on it unchanged. Transcripts have no analogue of
    XES's `resource`/`group`/`channel`/`department`, so those columns are filled with
    the nearest real thing (model, MCP server, git branch, cwd) rather than left empty
    or invented.

    Every default is a no-op filter. `min_trace_length=1`, `min_activity_cases=1` and
    `max_cases=None` mean an unmodified call returns the whole log, so a caller has to
    ask for filtering and the stats always record what they asked for.

    Args:
        rows: Output of `fetch`, or an equivalent list of dicts.
        granularity: Activity alphabet. `family` (default) keeps one level below an MCP
            aggregator: 102 activities corpus-wide, no identifier collisions. `tool`
            keeps raw names: 262 activities and 5 collision groups. `server` keeps the
            transport only: 74 activities, but merges unrelated gateway servers.
        min_trace_length: Drop cases with fewer events than this. A 1-event case
            contributes no directly-follows edge but does count against conformance,
            so raising this trades coverage for honesty about which cases were judged.
        min_activity_cases: Drop activities occurring in fewer distinct cases than
            this, then drop cases left shorter than `min_trace_length`. Trims the long
            tail of one-off tools ahead of the miner's own edge threshold.
        max_cases: Keep at most this many cases. The scale knob, for when 5,258 cases
            is more than a downstream step wants.
        sampling: How `max_cases` chooses. `random` (default) is a seeded uniform
            sample over case ids. `longest` keeps the longest traces, which is
            available because it is sometimes what you want but is *not* the default,
            because it is severely biased: on the full corpus, taking the 500 longest
            cases moved mined conformance from 0.81 to 0.06 at the same threshold. The
            longest sessions are the least representative ones, so a model mined from
            them describes almost no ordinary case.
        seed: Seed for `sampling="random"`, so a sampled run is reproducible.
    """
    shaper = _GRANULARITY.get(granularity)
    if shaper is None:
        raise ValueError(
            f"unknown granularity {granularity!r}; expected one of {list(_GRANULARITY)}"
        )
    if min_trace_length < 1:
        raise ValueError(f"min_trace_length must be >= 1, got {min_trace_length}")
    if min_activity_cases < 1:
        raise ValueError(f"min_activity_cases must be >= 1, got {min_activity_cases}")
    if max_cases is not None and max_cases < 1:
        raise ValueError(f"max_cases must be >= 1 or None, got {max_cases}")
    if sampling not in ("longest", "random"):
        raise ValueError(f"unknown sampling {sampling!r}; expected 'longest' or 'random'")

    frame = _raw_frame(rows)
    raw_events, raw_cases = frame.height, frame["case_id"].n_unique()
    frame = frame.with_columns(
        pl.col("tool_name").map_elements(shaper, return_dtype=pl.String).alias("activity")
    )
    raw_activities = frame["activity"].n_unique()

    dropped_rare_activities = dropped_rare_events = 0
    if min_activity_cases > 1:
        keep = (
            frame.group_by("activity")
            .agg(pl.col("case_id").n_unique().alias("cases"))
            .filter(pl.col("cases") >= min_activity_cases)["activity"]
        )
        before_activities, before_events = frame["activity"].n_unique(), frame.height
        frame = frame.filter(pl.col("activity").is_in(keep.implode()))
        dropped_rare_activities = before_activities - frame["activity"].n_unique()
        dropped_rare_events = before_events - frame.height

    dropped_short_cases = dropped_short_events = 0
    if min_trace_length > 1 or dropped_rare_events:
        # Rare-activity removal can shorten a case below the floor, so the length
        # filter runs after it even when the caller left the floor at its default.
        before_cases, before_events = frame["case_id"].n_unique(), frame.height
        frame = frame.filter(pl.len().over("case_id") >= min_trace_length)
        dropped_short_cases = before_cases - frame["case_id"].n_unique()
        dropped_short_events = before_events - frame.height

    dropped_sampled_cases = dropped_sampled_events = 0
    if max_cases is not None and frame["case_id"].n_unique() > max_cases:
        sized = frame.group_by("case_id").agg(pl.len().alias("events"))
        if sampling == "longest":
            chosen = sized.sort(["events", "case_id"], descending=[True, False]).head(max_cases)
        else:
            chosen = sized.sort("case_id").sample(max_cases, shuffle=True, seed=seed)
        before_cases, before_events = frame["case_id"].n_unique(), frame.height
        frame = frame.filter(pl.col("case_id").is_in(chosen["case_id"].implode()))
        dropped_sampled_cases = before_cases - frame["case_id"].n_unique()
        dropped_sampled_events = before_events - frame.height

    if frame.is_empty():
        raise ValueError(
            f"every event was filtered out: {raw_events} raw events, "
            f"min_trace_length={min_trace_length}, min_activity_cases={min_activity_cases}"
        )

    events = _finalise(frame)
    stats = TranscriptStats(
        raw_events=raw_events,
        raw_cases=raw_cases,
        raw_activities=raw_activities,
        events=events.height,
        cases=events["case_id"].n_unique(),
        activities=events["activity"].n_unique(),
        granularity=granularity,
        min_trace_length=min_trace_length,
        min_activity_cases=min_activity_cases,
        max_cases=max_cases,
        sampling=sampling,
        dropped_short_cases=dropped_short_cases,
        dropped_short_events=dropped_short_events,
        dropped_rare_activities=dropped_rare_activities,
        dropped_rare_events=dropped_rare_events,
        dropped_sampled_cases=dropped_sampled_cases,
        dropped_sampled_events=dropped_sampled_events,
        identifier_collisions=collisions(events["activity"].to_list()),
    )
    return events, stats


def _raw_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    """Rename `claude-sql`'s columns to the log vocabulary, keeping timestamps as text.

    Parsing is deferred to `_finalise` so the same code path handles rows from the CLI
    (ISO strings) and from a committed fixture.
    """
    if not rows:
        raise ValueError("no tool calls returned; check the --glob pattern")
    frame = pl.DataFrame(rows, infer_schema_length=None)
    required = {"session_id", "ts", "tool_name"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"rows are missing required columns: {sorted(missing)}")

    present = set(frame.columns)
    return frame.select(
        pl.col("session_id").cast(pl.String).alias("case_id"),
        pl.col("tool_name").cast(pl.String),
        pl.col("ts").cast(pl.String).alias("timestamp"),
        _text("model", present).alias("resource"),
        _text("tool_use_id", present).alias("tool_use_id"),
        _text("cwd", present).alias("department"),
        _text("git_branch", present).alias("channel"),
    ).drop_nulls(["case_id", "tool_name", "timestamp"])


def _text(column: str, present: set[str]) -> pl.Expr:
    """The column as a string, or an empty string when the caller did not supply it.

    Only `session_id`, `ts` and `tool_name` are required. The enrichment columns come
    from joins that a hand-built or minimal row set will not have, and an adapter that
    demanded them would be harder to test than the thing it adapts.
    """
    if column not in present:
        return pl.lit("", dtype=pl.String).alias(column)
    return pl.col(column).cast(pl.String).fill_null("")


def _finalise(frame: pl.DataFrame) -> pl.DataFrame:
    """Parse timestamps, order events, and emit `parse_xes`'s exact nine columns.

    `claude-sql` returns naive UTC timestamps (verified against the wall clock), so
    unlike XES there is no offset to reconcile. The tolerant format list covers
    DuckDB's variable-precision rendering, and the tool_use_id tiebreak makes
    `position` deterministic across the handful of same-millisecond parallel calls.
    """
    parsed = frame.with_columns(
        pl.coalesce(
            pl.col("timestamp").str.to_datetime(
                format="%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="us"
            ),
            pl.col("timestamp").str.to_datetime(
                format="%Y-%m-%dT%H:%M:%S%.f", strict=False, time_unit="us"
            ),
            pl.col("timestamp").str.to_datetime(
                format="%Y-%m-%d %H:%M:%S", strict=False, time_unit="us"
            ),
        ).alias("ts")
    ).drop_nulls("ts")
    if parsed.is_empty():
        raise ValueError("no timestamp parsed; expected 'YYYY-MM-DD HH:MM:SS[.ffffff]'")

    # `group` mirrors the MCP server so the resource-style columns stay meaningful on a
    # log whose activities have already been collapsed to a family.
    return (
        parsed.with_columns(
            pl.col("tool_name").map_elements(tool_server, return_dtype=pl.String).alias("group")
        )
        .sort(["case_id", "ts", "tool_use_id"])
        .with_columns(
            pl.int_range(1, pl.len() + 1).over("case_id").cast(pl.Int32).alias("position")
        )
        .select(
            "case_id",
            "activity",
            "timestamp",
            "resource",
            "group",
            "channel",
            "department",
            "ts",
            "position",
        )
    )


def load(
    glob: str = FLEET_GLOB,
    *,
    granularity: Granularity = "family",
    min_trace_length: int = 1,
    min_activity_cases: int = 1,
    max_cases: int | None = None,
    sampling: Sampling = "random",
    seed: int = 0,
    timeout: int = 600,
) -> tuple[pl.DataFrame, TranscriptStats]:
    """Read live transcripts and shape them: `fetch` then `to_events`.

    The corpus grows while the agent runs, so two calls minutes apart return different
    counts. Any number derived from this must be reported with the timestamp it was
    taken, which is why `TranscriptStats` records the raw totals it started from.
    """
    return to_events(
        fetch(glob, timeout=timeout),
        granularity=granularity,
        min_trace_length=min_trace_length,
        min_activity_cases=min_activity_cases,
        max_cases=max_cases,
        sampling=sampling,
        seed=seed,
    )


def load_sample(path: Path, **kwargs: object) -> tuple[pl.DataFrame, TranscriptStats]:
    """Shape a committed JSON sample, so the logic is testable without live data."""
    return to_events(json.loads(Path(path).read_text()), **kwargs)  # type: ignore[arg-type]
