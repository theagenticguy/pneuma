"""Does an agent's own tool-use transcript work as a second event log?

The pipeline has only ever been validated on curated XES: a human business process,
short activity names, trace lengths that cluster. `transcriptlog` supplies a
structurally different fixture from Claude Code transcripts, and these tests are the
claim that the shaping is right rather than that the result is pretty.

Most tests run against `data/transcripts_sample.json`, a 75-event / 5-case slice of the
real fleet corpus with case ids and tool_use_ids anonymised and the tool names and
event order left exactly as they occurred. That keeps the shaping logic testable with
no `claude-sql` on PATH and no dependence on a corpus that grows while the suite runs.
The live-data tests skip when the CLI is absent, and assert only invariants that hold at
any corpus size, never a count, because two runs minutes apart see different logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from pneuma.casestudy import eventlog, miner, rules, transcriptlog

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "transcripts_sample.json"
PERMITS = ROOT / "data" / "receipt.xes"

pytestmark = pytest.mark.skipif(not SAMPLE.is_file(), reason="needs data/transcripts_sample.json")


@pytest.fixture(scope="module")
def sample() -> pl.DataFrame:
    events, _ = transcriptlog.load_sample(SAMPLE)
    return events


@pytest.fixture(scope="module")
def sample_stats() -> transcriptlog.TranscriptStats:
    _, stats = transcriptlog.load_sample(SAMPLE)
    return stats


# ── The contract: indistinguishable from parse_xes downstream ──


@pytest.mark.skipif(not PERMITS.is_file(), reason="needs data/receipt.xes")
def test_the_adapter_matches_parse_xes_column_for_column(sample: pl.DataFrame) -> None:
    """The whole point of the adapter. Same names, same dtypes, same order.

    Asserted against the real XES parser rather than a hardcoded list, so if a sibling
    changes `parse_xes`'s schema this test fails instead of the adapter silently
    drifting out of contract.
    """
    reference = eventlog.parse_xes(PERMITS)
    assert sample.columns == reference.columns
    assert sample.schema == reference.schema


def test_miner_mine_accepts_the_adapter_output(sample: pl.DataFrame) -> None:
    """A valid `Process` from transcript data, with no change to the miner."""
    discovery = miner.mine(sample, name="TranscriptSample", min_edge_cases=2)
    assert discovery.process.states
    assert discovery.process.transitions
    assert any(state.terminal for state in discovery.process.states)
    assert 0.0 <= discovery.coverage <= 1.0
    # Every transition names a declared state: `Process` validates this, so reaching
    # here at all means the IR is referentially sound.
    known = {state.name for state in discovery.process.states}
    assert all({t.source, t.target} <= known for t in discovery.process.transitions)


def test_the_rest_of_the_pipeline_reads_it_too(sample: pl.DataFrame, tmp_path: Path) -> None:
    """`eventlog.stats`, `persist_events`, `read_events` and `rules` are all unchanged."""
    stats = eventlog.stats(sample)
    assert stats.events == sample.height
    assert stats.cases == sample["case_id"].n_unique()

    connection = eventlog.connect(tmp_path / "transcripts.db")
    eventlog.init_schema(connection)
    assert eventlog.persist_events(connection, sample) == sample.height
    assert eventlog.read_events(connection).height == sample.height
    connection.close()

    # min_support=2 on a 5-case sample: this asserts the scan runs on transcript data,
    # not that any precedence found in 5 cases means anything.
    assert isinstance(rules.derive_precedences(sample, min_support=2), list)


def test_position_is_dense_and_ordered_within_each_case(sample: pl.DataFrame) -> None:
    """`directly_follows` shifts by `position`, so a gap would fabricate an edge."""
    for (case_id,), group in sample.group_by(["case_id"], maintain_order=True):
        positions = group.sort("position")["position"].to_list()
        assert positions == list(range(1, len(positions) + 1)), case_id
        timestamps = group.sort("position")["ts"].to_list()
        assert timestamps == sorted(timestamps), case_id


def test_shaping_is_deterministic_despite_same_millisecond_calls() -> None:
    """Parallel tool calls share a timestamp, so `position` needs a stable tiebreak.

    Without one, two runs over the same input can order a tied pair differently and
    produce different directly-follows edges from identical data.
    """
    tied = [
        {
            "session_id": "c1",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Bash",
            "tool_use_id": "t3",
        },
        {
            "session_id": "c1",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Read",
            "tool_use_id": "t1",
        },
        {
            "session_id": "c1",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Edit",
            "tool_use_id": "t2",
        },
        {
            "session_id": "c1",
            "ts": "2026-07-30 12:00:01.000",
            "tool_name": "Write",
            "tool_use_id": "t4",
        },
    ]
    first, _ = transcriptlog.to_events(tied)
    second, _ = transcriptlog.to_events(list(reversed(tied)))
    assert first["activity"].to_list() == second["activity"].to_list()
    assert first["activity"].to_list() == ["Read", "Edit", "Bash", "Write"]


# ── Granularity: the miner's 40-char truncation really does collide ──


def test_raw_tool_names_collide_under_the_miners_truncation() -> None:
    """`_identifier` truncates to 40 chars and MCP names share long prefixes.

    The two names below differ only from character 49, so they are the same identifier.
    This was assumed harmless on the two XES logs; on transcripts it is not, and the
    honest thing is to pin the collision rather than describe it.
    """
    colliding = [
        "mcp__gateway__amazon-sharepoint-mcp_sharepoint_read_file",
        "mcp__gateway__amazon-sharepoint-mcp_sharepoint_resolve_url",
    ]
    assert miner._identifier(colliding[0]) == miner._identifier(colliding[1])
    found = transcriptlog.collisions(colliding)
    assert len(found) == 1
    assert sorted(next(iter(found.values()))) == sorted(colliding)


def test_the_default_granularity_removes_the_collision(
    sample_stats: transcriptlog.TranscriptStats,
) -> None:
    """The committed sample contains the colliding SharePoint pair on purpose."""
    raw = [row["tool_name"] for row in json.loads(SAMPLE.read_text())]
    assert transcriptlog.collisions(raw), "the fixture should contain a real collision"
    assert sample_stats.granularity == "family"
    assert sample_stats.identifier_collisions == {}


def test_tool_granularity_breaks_the_miner_and_the_stats_predict_it() -> None:
    """The failure mode `family` exists to avoid, pinned end to end.

    `Process` rejects duplicate state names, so a colliding alphabet is a crash rather
    than a silently merged state. `TranscriptStats.identifier_collisions` reports it
    before mining, which is the difference between a diagnosis and a stack trace.
    """
    events, stats = transcriptlog.load_sample(SAMPLE, granularity="tool")
    assert stats.identifier_collisions, "stats should predict the crash"

    # The crash is conditional on the threshold, which is what makes the stats worth
    # having. Both colliding activities are rare, so a high `min_edge_cases` drops one
    # of them and the model builds fine; lower the threshold enough to keep both and
    # the same log stops mining. A caller tuning the threshold downward would meet this
    # with no warning, so the collision has to be reported at load time rather than
    # discovered when a particular threshold happens to surface it.
    with pytest.raises(Exception, match="duplicate state names"):
        miner.mine(events, name="RawTools", min_edge_cases=1)
    assert miner.mine(events, name="RawTools", min_edge_cases=2).process.states


def test_the_three_granularities_are_ordered_coarsest_last() -> None:
    """`server` collapses more than `family`, which collapses more than `tool`."""
    sizes = {
        granularity: transcriptlog.load_sample(SAMPLE, granularity=granularity)[1].activities
        for granularity in ("tool", "family", "server")
    }
    assert sizes["tool"] > sizes["family"] > sizes["server"]


@pytest.mark.parametrize(
    ("tool_name", "family", "server"),
    [
        ("Bash", "Bash", "Bash"),
        ("mcp__bonk__list_runs", "mcp:bonk", "mcp:bonk"),
        # An aggregator flattens its sub-server into the leaf with '_', so keeping one
        # level below it is what separates Slack from Outlook.
        ("mcp__gateway__slack-mcp_get_thread", "mcp:gateway:slack-mcp", "mcp:gateway"),
        (
            "mcp__plugin_personal-plugins_aws-outlook-mcp__email_read",
            "mcp:plugin_personal-plugins_aws-outlook-mcp:email",
            "mcp:plugin_personal-plugins_aws-outlook-mcp",
        ),
    ],
)
def test_family_and_server_collapse_each_naming_convention(
    tool_name: str, family: str, server: str
) -> None:
    """Three undocumented MCP naming conventions coexist in the corpus."""
    assert transcriptlog.tool_family(tool_name) == family
    assert transcriptlog.tool_server(tool_name) == server


def test_family_keeps_unrelated_gateway_servers_apart() -> None:
    """The reason `server` is not the default: it would merge Slack into Outlook."""
    slack = "mcp__gateway__slack-mcp_get_thread"
    outlook = "mcp__gateway__aws-outlook-mcp_email_read"
    assert transcriptlog.tool_family(slack) != transcriptlog.tool_family(outlook)
    assert transcriptlog.tool_server(slack) == transcriptlog.tool_server(outlook)


# ── No silent caps: every filter is counted ──


def test_the_defaults_filter_nothing(sample_stats: transcriptlog.TranscriptStats) -> None:
    """An unmodified call must return the whole log, or the stats mean nothing."""
    raw_rows = json.loads(SAMPLE.read_text())
    assert sample_stats.raw_events == len(raw_rows)
    assert sample_stats.events == sample_stats.raw_events
    assert sample_stats.cases == sample_stats.raw_cases
    assert sample_stats.dropped_events == 0
    assert sample_stats.dropped_share == 0.0
    assert (sample_stats.min_trace_length, sample_stats.min_activity_cases) == (1, 1)
    assert sample_stats.max_cases is None


def test_every_dropped_event_is_accounted_for_by_a_named_filter() -> None:
    """The defect class this module is defending against.

    A filter that reduced the log without the reduction appearing in the stats would
    let a caller present a flattering coverage number derived from a log they never
    saw. The three `dropped_*` pairs must sum to the total loss.
    """
    rows = [
        {
            "session_id": "long",
            "ts": f"2026-07-30 12:00:{i:02d}.000",
            "tool_name": "Bash",
            "tool_use_id": f"a{i}",
        }
        for i in range(10)
    ]
    rows += [
        {
            "session_id": "short",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Bash",
            "tool_use_id": "b0",
        },
        {
            "session_id": "rare",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Bash",
            "tool_use_id": "c0",
        },
        {
            "session_id": "rare",
            "ts": "2026-07-30 12:00:01.000",
            "tool_name": "OneOffTool",
            "tool_use_id": "c1",
        },
        {
            "session_id": "rare",
            "ts": "2026-07-30 12:00:02.000",
            "tool_name": "Bash",
            "tool_use_id": "c2",
        },
    ]
    _, stats = transcriptlog.to_events(rows, min_trace_length=3, min_activity_cases=2)

    assert stats.raw_events == 14
    assert stats.dropped_rare_activities == 1  # OneOffTool appears in one case
    assert stats.dropped_rare_events == 1
    # Two cases fall below the floor: 'short' was always length 1, and 'rare' drops to
    # length 2 once its one-off tool is removed. The second is the interesting one, and
    # it is why the two filters cannot be reported as one number.
    assert stats.dropped_short_cases == 2
    assert stats.dropped_short_events == 3
    accounted = (
        stats.dropped_short_events + stats.dropped_rare_events + stats.dropped_sampled_events
    )
    assert accounted == stats.dropped_events
    assert stats.events == 10
    assert stats.cases == 1


def test_removing_a_rare_activity_can_shorten_a_case_below_the_floor() -> None:
    """Filter order matters: the length check has to run after activity removal.

    A 3-event case whose middle event is a one-off tool becomes a 2-event case. If the
    length filter ran first, that case would survive at length 2 while the log claimed
    a floor of 3.
    """
    rows = [
        {
            "session_id": "keep",
            "ts": f"2026-07-30 12:00:{i:02d}.000",
            "tool_name": "Bash",
            "tool_use_id": f"k{i}",
        }
        for i in range(3)
    ]
    rows += [
        {
            "session_id": "thin",
            "ts": "2026-07-30 13:00:00.000",
            "tool_name": "Bash",
            "tool_use_id": "t0",
        },
        {
            "session_id": "thin",
            "ts": "2026-07-30 13:00:01.000",
            "tool_name": "Rare",
            "tool_use_id": "t1",
        },
        {
            "session_id": "thin",
            "ts": "2026-07-30 13:00:02.000",
            "tool_name": "Bash",
            "tool_use_id": "t2",
        },
    ]
    events, stats = transcriptlog.to_events(rows, min_trace_length=3, min_activity_cases=2)
    assert events["case_id"].unique().to_list() == ["keep"]
    assert stats.dropped_short_cases == 1
    assert min(events.group_by("case_id").len()["len"]) >= 3


def test_random_sampling_is_the_default_and_is_reproducible() -> None:
    """`longest` is available but biased, so the default must not be `longest`."""
    rows = [
        {
            "session_id": f"c{case}",
            "ts": f"2026-07-30 12:{case:02d}:{i:02d}.000",
            "tool_name": "Bash",
            "tool_use_id": f"{case}-{i}",
        }
        for case in range(10)
        for i in range(case + 1)  # case 0 has 1 event, case 9 has 10
    ]
    _, defaulted = transcriptlog.to_events(rows, max_cases=3)
    assert defaulted.sampling == "random"

    first, _ = transcriptlog.to_events(rows, max_cases=3, seed=7)
    second, _ = transcriptlog.to_events(rows, max_cases=3, seed=7)
    assert first["case_id"].unique().sort().to_list() == second["case_id"].unique().sort().to_list()

    longest, stats = transcriptlog.to_events(rows, max_cases=3, sampling="longest")
    assert longest["case_id"].unique().sort().to_list() == ["c7", "c8", "c9"]
    assert stats.dropped_sampled_cases == 7
    assert stats.dropped_sampled_events == sum(range(1, 8))
    assert "biased" in stats.report()


def test_the_report_names_every_knob_and_every_drop() -> None:
    rows = [
        {
            "session_id": f"c{c}",
            "ts": f"2026-07-30 12:00:{i:02d}.000",
            "tool_name": "Bash",
            "tool_use_id": f"{c}{i}",
        }
        for c in range(4)
        for i in range(c + 1)
    ]
    _, stats = transcriptlog.to_events(rows, min_trace_length=2, max_cases=2)
    report = stats.report()
    assert "min_trace_length=2" in report
    assert "max_cases=2" in report
    assert "short traces" in report
    assert "case sampling" in report


# ── Input validation ──


@pytest.mark.parametrize(
    "kwargs",
    [
        {"granularity": "nonsense"},
        {"min_trace_length": 0},
        {"min_activity_cases": 0},
        {"max_cases": 0},
        {"sampling": "nonsense"},
    ],
)
def test_bad_knobs_raise_rather_than_being_coerced(kwargs: dict[str, object]) -> None:
    rows = [
        {
            "session_id": "c",
            "ts": "2026-07-30 12:00:00.000",
            "tool_name": "Bash",
            "tool_use_id": "x",
        }
    ]
    with pytest.raises(ValueError):
        transcriptlog.to_events(rows, **kwargs)  # type: ignore[arg-type]


def test_an_empty_or_malformed_result_raises_instead_of_returning_a_blank_log() -> None:
    with pytest.raises(ValueError, match="no tool calls"):
        transcriptlog.to_events([])
    with pytest.raises(ValueError, match="missing required columns"):
        transcriptlog.to_events([{"session_id": "c", "ts": "2026-07-30 12:00:00.000"}])
    with pytest.raises(ValueError, match="every event was filtered out"):
        transcriptlog.to_events(
            [
                {
                    "session_id": "c",
                    "ts": "2026-07-30 12:00:00.000",
                    "tool_name": "Bash",
                    "tool_use_id": "x",
                }
            ],
            min_trace_length=5,
        )


def test_unparseable_timestamps_are_reported_not_dropped_to_empty() -> None:
    rows = [{"session_id": "c", "ts": "not-a-timestamp", "tool_name": "Bash", "tool_use_id": "x"}]
    with pytest.raises(ValueError, match="no timestamp parsed"):
        transcriptlog.to_events(rows)


def test_optional_enrichment_columns_may_be_absent() -> None:
    """`claude-sql` supplies model/cwd/git_branch, but the contract cannot require them."""
    rows = [
        {"session_id": "c", "ts": "2026-07-30 12:00:00.000", "tool_name": "Bash"},
        {"session_id": "c", "ts": "2026-07-30 12:00:01.000", "tool_name": "Read"},
    ]
    events, _ = transcriptlog.to_events(rows)
    assert events.height == 2
    assert events["resource"].to_list() == ["", ""]


# ── Live corpus: shape invariants only, never a count ──


@pytest.mark.skipif(not transcriptlog.available(), reason="needs claude-sql on PATH")
def test_the_live_fleet_corpus_shapes_and_mines() -> None:
    """The corpus grows while this runs, so nothing here asserts a magnitude."""
    events, stats = transcriptlog.load(transcriptlog.FLEET_GLOB)
    assert stats.events == events.height
    assert stats.dropped_events == 0, "the default path must not filter"
    assert stats.identifier_collisions == {}, "family granularity should not collide"

    discovery = miner.mine(events, name="BonkFleet", min_edge_cases=5)
    assert discovery.process.transitions
    assert 0.0 <= discovery.coverage <= 1.0
