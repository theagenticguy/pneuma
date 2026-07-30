"""A `MemoryBackend` over Turso: addressable entries, vector recall, score learning.

Replaces `JSONMemoryBackend` in this project's learning loops. Three things the
project needed happened to be the same object.

**Targeted recall.** A single prose playbook is recalled whole or not at all, so
a gradient about one piece of advice is routed to a blob containing all of it
and the consolidating model rewrites whatever it likes. Splitting the playbook
into addressable entries and retrieving the few that bear on the current
decision makes the gradient land on those entries and no others. That is the
whole point of `ParameterMeta["results"]`, and section "Narrow gradients" below
is the mechanism.

**One artifact.** This project's audit database is already libSQL (see
`casestudy.eventlog`). Putting learned parameters in the same engine means the
event log, the mined model, its verification verdict, the runs executed under
it, and the parameters the runs learned are one file somebody can be handed.

**Two learning channels.** `GradFeedback` carries `text` and `score`. A
text-rewriting backend ignores the score and a score-learning host ignores the
text; because `MemoryBackend` already satisfies `ParameterHost`, one
`_consolidate` gets both. Text entries are rewritten. Numeric parameters — a
support threshold, a step budget, anything an agent might propose for its own
harness — are moved by the score, using the search in `_numeric_update`. No
separate host class.

## Retrieval is vector search, and FTS could not have replaced it

Measured with Cohere Embed v4 vectors over six navigator playbook entries, by
`tests/test_turso_memory.py::test_live_retrieval_discriminates_on_a_real_playbook`,
which prints exactly this:

    query                                            top-1  distance
    "I am at a state I have seen before, what now"    OK     0.6155
    "which of these two moves ends the case"          OK     0.6541
    "can I file the appeal now"                       OK     0.5388
    "the customer still owes money"                   OK     0.6210
    "keeps going around in circles between steps"     MISS   0.7219

    4/5 top-1, 5/5 recalled at k=2
    relevant mean 0.6303 against control mean 0.8683, separation +0.2381

Four of five, and the fifth is why `learning.TOP_K` is greater than one: the
correct entry is retrieved, just not first. The failing probe is kept in the
test rather than removed, because choosing the questions is how a retrieval
measurement flatters itself.

Turso's FTS does exist (`CREATE INDEX ... USING fts (...)` behind
`experimental_features='index_method'`, Tantivy-backed) but `fts_score()`
returns 0.0 for every matching row, so it cannot rank and the ABC's `k` would
select an arbitrary subset. `test_turso_fts_cannot_rank_...` records that, and
it needs no credentials because it is a property of the database. Nothing here
depends on FTS.

Two corrections to notes this was built from, both found by probing: the match
syntax is `WHERE column MATCH ?` (the `fts_match(index_name, ...)` form does not
parse, "no such column"), and `vector_distance_cos` raises
`Conversion error: Invalid vector type` on a NULL blob rather than returning
NULL, which is why every retrieval query is an inner join.

## The defect this file is designed against

An embedding backend fails soft. It always returns something ranked, so
"retrieval returned garbage" and "retrieval worked" are the same observation
from the outside. Shipping that unguarded rebuilds, inside the memory layer,
exactly the vacuity problem `pneuma.detect` exists to catch: the loop would
report healthy rounds while learning from advice that had nothing to do with
the decision.

So retrieval quality is *measured*, by `probe_retrieval`, and the measurement
is a discrimination test rather than a smoke test. It asks whether relevant
queries land closer than deliberately unrelated ones, and reports the margin
between the two distributions. When they overlap it says so and refuses to
certify, in the same three-valued spirit as `detect.vacuity`: not knowing is
not a pass. `test_probe_retrieval_detects_a_useless_embedding` is the proof it
has teeth — against a constant embedder, `search` still returns a full ranked
list of the requested length, so every smoke test passes and only the
separation reveals there is no signal.

`distance_ceiling` is off by default and it is not a default anybody should
guess. On the corpus above the gap is real but narrow, and it is a property of
that corpus rather than of the embedding model: `calibrate_ceiling` on the same
five probes returns 0.7757, which no one would have picked. So the ceiling is
derived from measurement, and refused outright when the distributions overlap.
Setting one by taste is the silent cap this project keeps finding.

## Narrow gradients

The chain, and every link is load-bearing:

1. `search` returns a `ParameterView` whose `meta["results"]` is
   `{entry_id: value}` for the retrieved entries only.
2. `MemoryBackend.search` merges that into the `ParameterRecalledEvent`'s meta.
3. `build_graph` copies the event's meta onto the `ParameterNode`.
4. `TextGradOptimizer.consolidate` merges `meta["results"]` across the
   consolidation group and passes it as `retrieved=`.
5. `_consolidate` shows the consolidating agent *only* those entries and hands
   it CRUD tools keyed by entry id.

Entry ids come from a persisted monotonic counter and are never reused, so an
id recorded during the forward pass still names the same logical entry at
consolidation time, across saves, deletes, and reopens.

Two sharp edges the library documents and this file honours. A `ParameterView`
is single-use — "one logical recall, one event" — so callers must recall per
call, not once per batch. And a gradient target is discovered in *call
arguments*, so a view must be passed as a handle: `f"{view}"` computes the same
prompt with the edge silently dropped.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args, get_origin

import turso
from ai_functions.ai_thread import ai_function
from ai_functions.ai_thread.postcondition import PostConditionResult
from ai_functions.memory.base import DynamicToolProvider, MemoryBackend, ParameterMeta
from ai_functions.memory.procedural import validate_procedural
from strands.tools import ToolProvider
from strands.tools.decorator import (
    tool as _strands_tool,  # pyright: ignore[reportUnknownVariableType]
)

from .embedding import (
    CACHE_SCHEMA,
    DOCUMENT,
    QUERY,
    BedrockCohereEmbedder,
    Embedder,
    EmbeddingCache,
    digest_of,
    fetch_one,
    fetch_rows,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ai_functions.types.graph import GradFeedback
    from pydantic import BaseModel
    from strands.models import Model
    from strands.types.tools import AgentTool


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memory_entry (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  entry_id   TEXT NOT NULL,
  position   INTEGER NOT NULL,
  value      TEXT NOT NULL,
  digest     TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_entry_order ON memory_entry(actor_id, param, position);

CREATE TABLE IF NOT EXISTS memory_scalar (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param)
);

CREATE TABLE IF NOT EXISTS memory_counter (
  actor_id TEXT NOT NULL,
  param    TEXT NOT NULL,
  next_id  INTEGER NOT NULL,
  PRIMARY KEY (actor_id, param)
);

CREATE TABLE IF NOT EXISTS memory_score_observation (
  actor_id   TEXT NOT NULL,
  param      TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  value      REAL NOT NULL,
  score      REAL NOT NULL,
  rationale  TEXT NOT NULL DEFAULT '',
  observed_at REAL NOT NULL,
  PRIMARY KEY (actor_id, param, seq)
);

{CACHE_SCHEMA}
"""


# ── Consolidation AI functions ──
#
# `coordinator_tools_enabled=False` on every one: memory-internal machinery must
# not discover or message threads. Its only surface is the memory tools.


def _check_valid_python(response: str) -> PostConditionResult:
    """Post-condition: consolidated procedural code must parse as Python."""
    try:
        validate_procedural(response)
    except SyntaxError as error:
        return PostConditionResult(passed=False, message=f"Code is not valid Python: {error}")
    return PostConditionResult(passed=True)


@ai_function[str](structured_output=False, coordinator_tools_enabled=False)
def _edit_entries(retrieved: str, feedback: str, description: str = "") -> str:
    """Drive entry-level consolidation: edit the retrieved entries with the tools."""
    return (
        "You are a memory manager. The entries below are the ones a forward pass "
        "actually retrieved, so they are the entries this feedback is about. Use the "
        "tools to update, add, or delete entries so the feedback is incorporated.\n\n"
        f"{_format_block(description)}"
        f"<retrieved_entries>\n{retrieved}\n</retrieved_entries>\n\n"
        f"<feedback>\n{feedback}\n</feedback>\n\n"
        "Rules:\n"
        "- Follow the format_instructions above when present.\n"
        "- Address entries by the entry_id shown. Update in place rather than adding a "
        "near-duplicate; the ids are what make a later gradient land narrowly.\n"
        "- Keep each entry one self-contained piece of advice. An entry that bundles "
        "several unrelated points cannot be retrieved for one of them.\n"
        "- Do not edit entries that were not retrieved and are not shown.\n"
        '- When every change is applied, answer exactly "done".'
    )


@ai_function[str](coordinator_tools_enabled=False)
def _rewrite_value(value: str, feedback: list[str], description: str = "") -> str:
    """Build the prompt to merge feedback into a scalar text parameter."""
    return (
        "Update the following value with the feedback provided.\n"
        "Return only the updated value.\n\n"
        f"{_format_block(description)}"
        f"<value>\n{value}\n</value>\n\n"
        f"<feedback>\n{_bullets(feedback)}\n</feedback>"
    )


@ai_function[str](post_conditions=[_check_valid_python], coordinator_tools_enabled=False)
def _rewrite_code(value: str, feedback: list[str], description: str = "") -> str:
    """Build the prompt to merge feedback into a `Procedural` code parameter."""
    return (
        "Update the following Python code with the feedback provided.\n"
        "Return the complete updated code, and nothing else.\n\n"
        f"{_format_block(description)}"
        f"<code>\n{value}\n</code>\n\n"
        f"<feedback>\n{_bullets(feedback)}\n</feedback>"
    )


@ai_function[str](coordinator_tools_enabled=False)
def _answer_over_value(value: str, query: str) -> str:
    """Build the prompt to answer a question over a parameter's content."""
    return (
        "Answer the question using only the content below.\n"
        f"<question>{query}</question>\n"
        f"<content>{value}</content>"
    )


def _bullets(values: Sequence[str]) -> str:
    # Gradient text often arrives already bulleted; strip one leading bullet so
    # entries never render as "- - add: ...".
    return "\n".join(f"- {v.strip().removeprefix('- ').strip()}" for v in values)


def _format_block(description: str) -> str:
    """Render the schema description as format instructions, or `""`.

    The description is where the schema author states what the parameter should
    contain and how updates should merge. Consolidation must honour it, not
    just the raw feedback.
    """
    if not description:
        return ""
    return f"<format_instructions>\n{description}\n</format_instructions>\n\n"


# ── Retrieval results ──


@dataclass(frozen=True)
class Retrieved:
    """One search hit: the entry, and how far it was from the query.

    `distance` is `vector_distance_cos`, so 0 is identical and 2 is opposite.
    Carried out of the backend because a caller that cannot see the distance
    cannot tell a confident hit from the best of a bad set, which is this
    file's central failure mode.
    """

    entry_id: str
    value: str
    distance: float


@dataclass(frozen=True)
class Discrimination:
    """Whether retrieval can tell a relevant query from an unrelated one.

    A pass/fail smoke test on retrieval would be the vacuity defect again: an
    embedding search always returns something, so "it returned results" is not
    evidence. What this records instead is a *separation*: how much closer
    queries whose answer is in the corpus land than queries whose answer is
    not. `discriminates` is three-valued for the same reason `detect.vacuity`'s
    `live` is — an unmeasurable case is not a pass.

    Attributes:
        relevant: `(query, expected_entry_id, top_entry_id, distance)` per probe.
        controls: `(query, top_entry_id, distance)` for queries with no answer here.
        hits: Probes whose expected entry came back first.
        recalled: Probes whose expected entry came back anywhere in the top k.
        self_retrieval_failures: Entries whose own text does not retrieve them
            first. A non-empty list means the index is broken, not merely weak.
    """

    relevant: tuple[tuple[str, str, str, float], ...]
    controls: tuple[tuple[str, str, float], ...]
    hits: int
    recalled: int
    self_retrieval_failures: tuple[str, ...]

    @property
    def probes(self) -> int:
        """Number of relevant probes measured."""
        return len(self.relevant)

    @property
    def mean_relevant_distance(self) -> float | None:
        """Mean best distance over relevant probes, or None with no probes."""
        if not self.relevant:
            return None
        return sum(d for *_, d in self.relevant) / len(self.relevant)

    @property
    def mean_control_distance(self) -> float | None:
        """Mean best distance over control queries, or None with no controls."""
        if not self.controls:
            return None
        return sum(d for *_, d in self.controls) / len(self.controls)

    @property
    def separation(self) -> float | None:
        """Control distance minus relevant distance; larger is better.

        None when either side was not measured. Zero or negative means the
        embedding cannot distinguish a query this corpus answers from one it
        does not, and every retrieval from it is unevidenced.
        """
        relevant, control = self.mean_relevant_distance, self.mean_control_distance
        if relevant is None or control is None:
            return None
        return control - relevant

    @property
    def worst_relevant(self) -> float | None:
        """Furthest a relevant probe's best hit landed."""
        return max((d for *_, d in self.relevant), default=None)

    @property
    def best_control(self) -> float | None:
        """Closest an unrelated query's best hit landed."""
        return min((d for *_, d in self.controls), default=None)

    @property
    def overlaps(self) -> bool | None:
        """Whether the two distance distributions overlap at all.

        Overlap does not make retrieval useless — the means can still separate
        cleanly — but it does mean no single distance threshold can divide
        relevant from unrelated, so `calibrate_ceiling` refuses.
        """
        worst, best = self.worst_relevant, self.best_control
        if worst is None or best is None:
            return None
        return worst >= best

    @property
    def discriminates(self) -> bool | None:
        """Does retrieval separate relevant from unrelated, at all?

        None when it was not measured (no probes, or no controls), because an
        unrun measurement is not a finding. False when the index is internally
        broken (an entry does not retrieve itself) or when relevant queries do
        not land closer on average than unrelated ones.
        """
        if self.self_retrieval_failures:
            return False
        if not self.relevant:
            return None
        separation = self.separation
        if separation is None:
            return None
        return separation > 0 and self.recalled > 0

    def __str__(self) -> str:
        if self.self_retrieval_failures:
            broken = ", ".join(self.self_retrieval_failures)
            return f"retrieval BROKEN: entries do not retrieve themselves ({broken})"
        if not self.relevant:
            return "retrieval UNMEASURED: no relevant probes were supplied"
        head = f"{self.hits}/{self.probes} top-1, {self.recalled}/{self.probes} recalled@k"
        if self.separation is None:
            return f"retrieval PARTIAL: {head}; no control queries, so separation is unmeasured"
        verdict = "DISCRIMINATES" if self.discriminates else "DOES NOT DISCRIMINATE"
        return (
            f"retrieval {verdict}: {head}; relevant {self.mean_relevant_distance:.4f} "
            f"vs control {self.mean_control_distance:.4f}, separation {self.separation:+.4f}"
            + (
                f"; distributions overlap ({self.worst_relevant:.4f} >= {self.best_control:.4f})"
                if self.overlaps
                else "; distributions do not overlap"
            )
        )


class CeilingNotSeparable(ValueError):
    """No single distance threshold divides relevant probes from control queries.

    Raised by `calibrate_ceiling` rather than returning a midpoint that would
    drop real hits or admit unrelated ones. A ceiling that cannot be justified
    by measurement is the silent cap this project keeps finding, and inventing
    one here would put it in the retrieval path.
    """


class TursoMemoryBackend(MemoryBackend):
    """Memory over Turso: addressable entries, vector retrieval, score learning.

    ## Public surface

    Storage and identity:
        `path`, `connection`, `backend_id`, `close`, `init_schema`

    Entries (list parameters), all keyed by never-reused ids:
        `list_entries(name) -> {entry_id: value}` in position order
        `add_entry(name, value) -> entry_id`
        `update_entry(name, entry_id, value) -> bool`
        `remove_entry(name, entry_id) -> bool`
        `search_entries(name, query, k) -> list[Retrieved]`

    Numeric parameters, learned from `GradFeedback.score`:
        `numeric_value(name) -> float`
        `observations(name) -> list[(value, score, rationale)]`

    Retrieval quality, because an embedding backend fails soft:
        `probe_retrieval(name, relevant, controls, k) -> Discrimination`
        `calibrate_ceiling(name, relevant, controls, k) -> float`
        `distance_ceiling` (settable; `None` means no cap, which is the default)

    Inherited from `MemoryBackend` and *not* overridden, deliberately:
        `recall`, `query`, `search`, `consolidate`, `save`, `fetch`, `delete`
        Overriding those instead of the `_*` hooks would skip
        `ParameterRecalledEvent` emission and parameters would vanish from the
        optimizer graph with no error at all.

    Args:
        schema: The Pydantic memory schema.
        actor_id: Namespace within the database; several actors share one file.
        path: Database file. Ignored when `connection` is supplied.
        model: Model for the consolidation and query AI functions.
        embedder: Embedding provider. Defaults to Cohere Embed v4 on Bedrock,
            constructed lazily so importing this module needs no credentials.
        distance_ceiling: Drop hits further than this. `None` (the default)
            caps nothing; derive a value with `calibrate_ceiling` instead of
            picking one.
        connection: An existing libSQL connection to share — e.g. the audit
            database from `casestudy.eventlog`, which is how parameters and
            evidence end up in one file. A shared connection is not closed by
            `close()`; the owner closes it.
    """

    def __init__(
        self,
        schema: type[BaseModel],
        actor_id: str,
        path: Path | str | None = None,
        model: Model | str | None = None,
        embedder: Embedder | None = None,
        distance_ceiling: float | None = None,
        connection: turso.Connection | None = None,
    ) -> None:
        super().__init__(schema, actor_id)
        if connection is None and path is None:
            raise ValueError("TursoMemoryBackend needs either a path or a connection.")

        self.path = Path(path) if path is not None else None
        self._owns_connection = connection is None
        self.connection = connection if connection is not None else connect(self.path)  # pyright: ignore[reportArgumentType]
        self.distance_ceiling = distance_ceiling

        self._edit_entries_fn = _edit_entries.replace(model=model)
        self._rewrite_value_fn = _rewrite_value.replace(model=model)
        self._rewrite_code_fn = _rewrite_code.replace(model=model)
        self._answer_fn = _answer_over_value.replace(model=model)

        self.init_schema()
        self.cache = EmbeddingCache(self.connection, embedder or BedrockCohereEmbedder())
        self._seed_defaults()

    # ── Storage setup ──

    def init_schema(self) -> None:
        """Create the memory tables if absent. Safe to call on a shared database."""
        for statement in filter(str.strip, SCHEMA.split(";")):
            self.connection.execute(statement)
        self.connection.commit()

    def _seed_defaults(self) -> None:
        """Write each parameter's schema default on first use of this actor.

        Without this a fresh actor's scalar read has nothing to return and the
        caller would see an empty string where the schema promised a seed value.
        Existing rows are never touched, so a reopen preserves what was learned.
        """
        defaults = self.schema()
        for name in self._leaf_parameter_names():
            value = _nested_attr(defaults, name)
            if self._is_list_field(name):
                if not self._entry_rows(name):
                    for item in value or []:
                        self.add_entry(name, str(item))
            elif not self._scalar_present(name):
                self._write_scalar(name, "" if value is None else str(value))
        self.connection.commit()

    def close(self) -> None:
        """Commit, and close the connection when this backend owns it.

        A shared connection is left open: the owner (`casestudy.eventlog`, say)
        is still using it, and closing somebody else's handle here would break
        the very colocation this backend exists for.
        """
        self.connection.commit()
        if self._owns_connection:
            self.connection.close()

    # ── Parameter classification ──

    def _is_numeric_field(self, name: str) -> bool:
        """Whether this parameter is an `int` or `float` learned from scores.

        The check tolerates `Optional[int]` and `Annotated[...]` because a
        schema author writing a harness parameter will reasonably use either,
        and misclassifying one would silently route it to the text path where
        a model would be asked to rewrite a number.
        """
        return _numeric_type(self._resolve_field(name).annotation) is not None

    def _numeric_bounds(self, name: str) -> tuple[float | None, float | None]:
        """Read `Ge`/`Gt`/`Le`/`Lt` from the field's metadata as a search domain.

        The schema's own constraints are the only trustworthy domain: they are
        what Pydantic will actually enforce, so a search that respects them
        cannot propose a value the schema then rejects.

        `gt` and `lt` are *exclusive*, and treating them as inclusive was a real
        bug found by probing: `Field(0.5, gt=0.0, lt=1.0)` under repeated
        zero scores proposed exactly 1.0 on the fourth round, which
        `M(r=1.0)` then refuses. The proposal was valid arithmetic and invalid
        data, and nothing in the loop would have said which. So an exclusive
        bound is pulled inward by `_EXCLUSIVE_EPSILON`: a search over an open
        interval has to stop somewhere short of the endpoint, and naming how far
        short is better than discovering it at validation time.

        For an `int` field the inward step is a whole unit, since `gt=0` means
        the smallest legal value is 1 and a fractional epsilon would round back
        onto the forbidden endpoint.
        """
        is_integer = _numeric_type(self._resolve_field(name).annotation) is int
        margin = 1.0 if is_integer else _EXCLUSIVE_EPSILON
        low: float | None = None
        high: float | None = None
        for marker in self._resolve_field(name).metadata:
            for attribute, inward in (("ge", 0.0), ("gt", margin)):
                bound = getattr(marker, attribute, None)
                if bound is not None:
                    candidate = float(bound) + inward
                    low = candidate if low is None else max(low, candidate)
            for attribute, inward in (("le", 0.0), ("lt", margin)):
                bound = getattr(marker, attribute, None)
                if bound is not None:
                    candidate = float(bound) - inward
                    high = candidate if high is None else min(high, candidate)
        return low, high

    # ── Entry storage ──

    def _require_list(self, name: str) -> None:
        if not self._is_list_field(name):
            raise TypeError(
                f"Entry operations are only supported for list parameters, but '{name}' is not one."
            )

    def _entry_rows(self, name: str) -> list[tuple[str, str]]:
        rows = fetch_rows(
            self.connection,
            "SELECT entry_id, value FROM memory_entry WHERE actor_id = ? AND param = ? "
            "ORDER BY position, entry_id",
            (self.actor_id, name),
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    def _entry_exists(self, name: str, entry_id: str) -> bool:
        return (
            fetch_one(
                self.connection,
                "SELECT 1 FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
                (self.actor_id, name, entry_id),
            )
            is not None
        )

    def list_entries(self, name: str) -> dict[str, str]:
        """Return `{entry_id: value}` for a list parameter, in position order."""
        self._require_list(name)
        return dict(self._entry_rows(name))

    def _alloc_id(self, name: str) -> str:
        """Allocate the next entry id: monotonic per parameter, never reused.

        Never reused is the property that makes narrow gradients survive a
        round. An id recorded in the forward pass's event log must still name
        the same logical entry when `consolidate` runs, and it will have
        survived saves, deletes, other consolidations, and a reopen by then.
        """
        row = fetch_one(
            self.connection,
            "SELECT next_id FROM memory_counter WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        current = int(row[0]) if row is not None else 1
        self.connection.execute(
            "INSERT OR REPLACE INTO memory_counter (actor_id, param, next_id) VALUES (?, ?, ?)",
            (self.actor_id, name, current + 1),
        )
        return str(current)

    def _next_position(self, name: str) -> int:
        row = fetch_one(
            self.connection,
            "SELECT COALESCE(MAX(position), -1) FROM memory_entry WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        return (int(row[0]) if row is not None else -1) + 1

    def add_entry(self, name: str, value: str) -> str:
        """Append an entry and return its stable id."""
        self._require_list(name)
        entry_id = self._alloc_id(name)
        self.connection.execute(
            "INSERT INTO memory_entry "
            "(actor_id, param, entry_id, position, value, digest, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.actor_id,
                name,
                entry_id,
                self._next_position(name),
                value,
                digest_of(value),
                time.time(),
            ),
        )
        self.connection.commit()
        return entry_id

    def update_entry(self, name: str, entry_id: str, value: str) -> bool:
        """Replace an entry's text, keeping its id. False when the id is unknown.

        The digest moves with the text, which is what makes the embedding cache
        self-invalidating: the rewritten entry no longer matches any cached
        vector, so the next search re-embeds it. A cache keyed by entry id
        instead would serve the pre-rewrite vector for post-rewrite text and
        the mistake would be invisible, because a vector search always returns
        something ranked.
        """
        self._require_list(name)
        if not self._entry_exists(name, entry_id):
            return False
        self.connection.execute(
            "UPDATE memory_entry SET value = ?, digest = ?, updated_at = ? "
            "WHERE actor_id = ? AND param = ? AND entry_id = ?",
            (value, digest_of(value), time.time(), self.actor_id, name, entry_id),
        )
        self.connection.commit()
        return True

    def remove_entry(self, name: str, entry_id: str) -> bool:
        """Delete an entry by id. The id is retired, never reused."""
        self._require_list(name)
        if not self._entry_exists(name, entry_id):
            return False
        self.connection.execute(
            "DELETE FROM memory_entry WHERE actor_id = ? AND param = ? AND entry_id = ?",
            (self.actor_id, name, entry_id),
        )
        self.connection.commit()
        return True

    # ── Vector retrieval ──

    def embed_pending(self, name: str) -> int:
        """Embed every entry of `name` that has no cached vector; return the count.

        Idempotent, and the only place entry text reaches the provider. Called
        by `search_entries`, but exposed so a caller can pay the embedding cost
        up front rather than inside a decision loop.
        """
        self._require_list(name)
        rows = fetch_rows(
            self.connection,
            "SELECT e.value FROM memory_entry e "
            "LEFT JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND c.digest IS NULL",
            (DOCUMENT, self.cache.embedder.model_id, self.actor_id, name),
        )
        pending = sorted({str(row[0]) for row in rows})
        if pending:
            self.cache.ensure(pending, DOCUMENT)
        return len(pending)

    def search_entries(self, name: str, query: str, k: int = 5) -> list[Retrieved]:
        """Return the top-k entries by `vector_distance_cos ASC`.

        Ranking happens in the database over a JOIN against the embedding
        cache, so no corpus is materialized in Python. Entries with no cached
        vector are embedded first; if one somehow still lacks a vector the
        inner join drops it, so `unranked_entries` exists to make that
        countable rather than invisible.

        `distance_ceiling`, when set, drops hits beyond it — including all of
        them. An honest empty result is the point: an agent handed the best of
        a bad set cannot tell it apart from good advice.
        """
        self._require_list(name)
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        if not self._entry_rows(name):
            return []

        self.embed_pending(name)
        query_vector = self.cache.vector(query, QUERY)

        rows = fetch_rows(
            self.connection,
            "SELECT e.entry_id, e.value, vector_distance_cos(c.embedding, ?) AS distance "
            "FROM memory_entry e "
            "JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? "
            "ORDER BY distance ASC LIMIT ?",
            (query_vector, DOCUMENT, self.cache.embedder.model_id, self.actor_id, name, k),
        )
        hits = [
            Retrieved(entry_id=str(row[0]), value=str(row[1]), distance=float(row[2]))
            for row in rows
        ]
        if self.distance_ceiling is not None:
            hits = [h for h in hits if h.distance <= self.distance_ceiling]
        return hits

    def unranked_entries(self, name: str) -> list[str]:
        """Entry ids with no cached vector, which a search silently cannot return.

        Should always be empty after `embed_pending`. It exists because the
        alternative to counting this is an inner join quietly shrinking the
        candidate set, which reads exactly like a corpus that never had the
        entry in it.
        """
        self._require_list(name)
        rows = fetch_rows(
            self.connection,
            "SELECT e.entry_id FROM memory_entry e "
            "LEFT JOIN memory_embedding_cache c "
            "  ON c.digest = e.digest AND c.input_type = ? AND c.model_id = ? "
            "WHERE e.actor_id = ? AND e.param = ? AND c.digest IS NULL",
            (DOCUMENT, self.cache.embedder.model_id, self.actor_id, name),
        )
        return [str(row[0]) for row in rows]

    # ── Retrieval discrimination ──

    def probe_retrieval(
        self,
        name: str,
        relevant: Sequence[tuple[str, str]],
        controls: Sequence[str] = (),
        k: int = 3,
    ) -> Discrimination:
        """Measure whether retrieval separates answerable queries from unrelated ones.

        The guard against this backend's characteristic failure. A vector
        search always returns a ranked list, so "search returned entries" is
        not evidence that retrieval works, and a loop built on that observation
        would report healthy rounds while learning from irrelevant advice.

        Three measurements, and the third is the one a smoke test omits:

        1. Relevant probes. `(query, expected_entry_id)` pairs, counted at
           top-1 and anywhere in the top k.
        2. Self-retrieval. Each entry's own text must retrieve that entry
           first. A failure here is an index defect, not a weak embedding, so
           it sets `discriminates` to False outright.
        3. Control queries. Questions this corpus does not answer. Their
           distances are the null distribution the relevant ones must beat.
           Without them there is no separation to report and the verdict is
           `None`, because a measurement that cannot fail is not a measurement.

        Ignores `distance_ceiling` throughout: the ceiling is derived from this
        measurement, so applying it here would be circular.
        """
        self._require_list(name)
        ceiling, self.distance_ceiling = self.distance_ceiling, None
        try:
            probes: list[tuple[str, str, str, float]] = []
            hits = recalled = 0
            for query, expected in relevant:
                found = self.search_entries(name, query, k=k)
                if not found:
                    probes.append((query, expected, "", float("inf")))
                    continue
                ids = [h.entry_id for h in found]
                hits += ids[0] == expected
                recalled += expected in ids
                probes.append((query, expected, ids[0], found[0].distance))

            control_rows: list[tuple[str, str, float]] = []
            for query in controls:
                found = self.search_entries(name, query, k=1)
                if found:
                    control_rows.append((query, found[0].entry_id, found[0].distance))

            failures = [
                entry_id
                for entry_id, value in self._entry_rows(name)
                if (own := self.search_entries(name, value, k=1)) and own[0].entry_id != entry_id
            ]
        finally:
            self.distance_ceiling = ceiling

        return Discrimination(
            relevant=tuple(probes),
            controls=tuple(control_rows),
            hits=hits,
            recalled=recalled,
            self_retrieval_failures=tuple(failures),
        )

    def calibrate_ceiling(
        self,
        name: str,
        relevant: Sequence[tuple[str, str]],
        controls: Sequence[str],
        k: int = 3,
        margin: float = 0.5,
    ) -> float:
        """Derive a `distance_ceiling` from measurement, or refuse.

        The ceiling sits between the furthest relevant hit and the closest
        control hit, at `margin` of the way across the gap. When those two
        distributions overlap there is no such point, and this raises rather
        than returning a midpoint that would either drop real hits or admit
        unrelated ones. A threshold nobody measured is a silent cap.

        Raises:
            CeilingNotSeparable: The distributions overlap, or one side was
                not measured, so no threshold is justified.
        """
        if not 0.0 <= margin <= 1.0:
            raise ValueError(f"margin must be in [0, 1], got {margin}")
        report = self.probe_retrieval(name, relevant, controls, k=k)
        worst, best = report.worst_relevant, report.best_control
        if worst is None or best is None:
            raise CeilingNotSeparable(
                "Calibration needs both relevant probes and control queries; "
                f"got {len(report.relevant)} and {len(report.controls)}."
            )
        if worst >= best:
            raise CeilingNotSeparable(
                f"No threshold separates relevant from unrelated for '{name}': the furthest "
                f"relevant hit is {worst:.4f} and the closest unrelated hit is {best:.4f}. "
                "Any ceiling either drops real hits or admits noise. Split entries that "
                "bundle several points, or add control queries closer to the domain."
            )
        return worst + margin * (best - worst)

    # ── Numeric parameters, learned from scores ──

    def numeric_value(self, name: str) -> float:
        """Current value of a numeric parameter, as a float."""
        if not self._is_numeric_field(name):
            raise TypeError(f"'{name}' is not a numeric parameter.")
        return float(self._read_scalar(name) or 0.0)

    def observations(self, name: str) -> list[tuple[float, float, str]]:
        """Every `(value, score, rationale)` recorded for a numeric parameter.

        The search's memory, and the audit trail for why a harness parameter
        holds the value it does. Persisted, so a reopened database resumes the
        search instead of restarting it.
        """
        rows = fetch_rows(
            self.connection,
            "SELECT value, score, rationale FROM memory_score_observation "
            "WHERE actor_id = ? AND param = ? ORDER BY seq",
            (self.actor_id, name),
        )
        return [(float(r[0]), float(r[1]), str(r[2])) for r in rows]

    def _record_observation(self, name: str, value: float, score: float, rationale: str) -> None:
        row = fetch_one(
            self.connection,
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM memory_score_observation "
            "WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        self.connection.execute(
            "INSERT INTO memory_score_observation "
            "(actor_id, param, seq, value, score, rationale, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.actor_id,
                name,
                int(row[0]) if row is not None else 1,
                value,
                score,
                rationale,
                time.time(),
            ),
        )

    def _numeric_update(self, name: str, current: float, score: float) -> float:
        """Propose the next value of a numeric parameter from the scores so far.

        A deterministic one-dimensional search with a shrinking trust region,
        over the domain the schema itself declares. Deterministic because a
        random step cannot be asserted in a test, and a learning rule nobody
        can test is the thing this project keeps finding.

        Two moves:

        *Exploit.* When some previously tried value scored better than the one
        just measured, step halfway toward it. Bisection toward a point known
        to be better needs no derivative and cannot overshoot past it.

        *Explore.* When the current value is the best seen, step out by
        `span * TRUST * (1 - score) * DECAY ** trials`. Every factor earns its
        place. `1 - score` makes a well-served value barely move and a badly
        served one move far. `DECAY ** trials` makes the sequence converge
        instead of oscillating. `TRUST` caps the first step at a quarter of the
        domain, and it is there because omitting it was a measured bug: without
        it a `threshold: int = Field(20, ge=1, le=100)` scoring 0.2 proposed 99
        on the very first round, which is not a search, it is a jump to the
        boundary that happens to be far from a bad value. A trust region is
        what makes the step a *step*.

        The direction is away from the worst value tried, or up when nothing
        else has been tried, which is arbitrary but has to be something.

        A perfect score therefore does not move the value at all, and a
        constant score converges rather than wandering. Both are testable
        offline, and both are properties a "nudge it a bit" rule lacks.
        """
        low, high = self._numeric_bounds(name)
        span = (high - low) if (low is not None and high is not None) else max(abs(current), 1.0)

        history = self.observations(name)
        means: dict[float, list[float]] = {}
        for value, observed, _ in history:
            means.setdefault(value, []).append(observed)
        averaged = {v: sum(s) / len(s) for v, s in means.items()}

        best_value = max(averaged, key=lambda v: (averaged[v], -abs(v - current)))
        if averaged[best_value] > score and best_value != current:
            proposal = current + 0.5 * (best_value - current)
        else:
            step = (
                span
                * _TRUST_FRACTION
                * (1.0 - score)
                * (_EXPLORE_DECAY ** max(len(history) - 1, 0))
            )
            worst_value = min(averaged, key=lambda v: (averaged[v], abs(v - current)))
            direction = 1.0 if worst_value == current else (1.0 if current > worst_value else -1.0)
            proposal = current + direction * step

        # Round before clamping, not after: rounding an already-clamped value can
        # step back outside the bound (0.5 rounds to 1.0 under `lt=1.0`), and a
        # proposal outside its declared domain fails validation somewhere the
        # cause is no longer visible.
        if _numeric_type(self._resolve_field(name).annotation) is int:
            proposal = float(round(proposal))
        if low is not None:
            proposal = max(proposal, low)
        if high is not None:
            proposal = min(proposal, high)
        return proposal

    # ── Scalar storage ──

    def _scalar_present(self, name: str) -> bool:
        return (
            fetch_one(
                self.connection,
                "SELECT 1 FROM memory_scalar WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            is not None
        )

    def _read_scalar(self, name: str) -> str:
        row = fetch_one(
            self.connection,
            "SELECT value FROM memory_scalar WHERE actor_id = ? AND param = ?",
            (self.actor_id, name),
        )
        return str(row[0]) if row is not None else ""

    def _write_scalar(self, name: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO memory_scalar (actor_id, param, value, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (self.actor_id, name, value, time.time()),
        )

    # ── Abstract storage contract ──

    def _save(self, name: str, value: Any) -> None:  # pyright: ignore[reportExplicitAny]
        if self._is_list_field(name):
            # A wholesale replace retires the old entries. The counter is
            # monotonic, so the retired ids are never handed out again and a
            # stale id from an earlier forward pass resolves to nothing rather
            # than to somebody else's entry.
            self.connection.execute(
                "DELETE FROM memory_entry WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            for item in value or []:
                self.add_entry(name, str(item))
        else:
            if self._is_procedural(name):
                ast.parse(str(value))
            self._write_scalar(name, "" if value is None else str(value))
        self.connection.commit()

    def _recall(self, name: str) -> tuple[Any, ParameterMeta]:  # pyright: ignore[reportExplicitAny]
        """Return a parameter's full value.

        The list case returns every entry, which is a full recall by
        definition. Note what it does *not* return: per-entry ids in the meta.
        A full recall's gradient is about the whole parameter, so handing
        consolidation a retrieval context here would claim a narrowness the
        forward pass did not have.
        """
        if self._is_list_field(name):
            return [value for _, value in self._entry_rows(name)], {}
        raw = self._read_scalar(name)
        if self._is_numeric_field(name):
            numeric = _numeric_type(self._resolve_field(name).annotation)
            return (numeric(float(raw or 0.0)) if numeric else raw), {}
        return raw, {}

    def _query(self, name: str, query: str) -> tuple[str, ParameterMeta]:
        value, _ = self._recall(name)
        content = "\n".join(f"- {v}" for v in value) if isinstance(value, list) else str(value)
        return self._answer_fn.run_sync(value=content, query=query), {}

    def _search(
        self,
        name: str,
        query: str,
        k: int = 5,
        **kwargs: Any,  # pyright: ignore[reportExplicitAny]
    ) -> tuple[list[str], ParameterMeta]:
        """Return the top-k entry texts, with the ids in `meta["results"]`.

        `meta["results"]` is the whole mechanism for narrow gradients: it
        travels into the recall event, onto the reconstructed `ParameterNode`,
        and back out as `consolidate`'s `retrieved=`, so consolidation edits
        exactly the entries this forward pass read. `distances` rides along so a
        caller can audit retrieval quality from the event log alone, after the
        fact, without re-running anything.
        """
        del kwargs
        hits = self.search_entries(name, query, k=k)
        return [h.value for h in hits], {
            "results": {h.entry_id: h.value for h in hits},
            "distances": {h.entry_id: round(h.distance, 6) for h in hits},
            "distance_ceiling": self.distance_ceiling,
            "embedding_model": self.cache.embedder.model_id,
        }

    def _consolidate(
        self,
        name: str,
        feedback: list[GradFeedback],
        retrieved: dict[str, str] | None = None,
        **kwargs: Any,  # pyright: ignore[reportExplicitAny]
    ) -> None:
        """Fold gradients into a parameter, over whichever channel applies.

        Both channels of `GradFeedback` are used, on the parameters each one
        can actually inform:

        - A numeric parameter reads `score`. Asking a model to rewrite a
          support threshold produces a number with a justification and no
          evidence; the score is a measurement of how the current value
          performed, which is what a search over values can use. The text is
          kept as the observation's rationale, so the artifact records why.
        - A list parameter reads `text`, agentically, editing only the entries
          `retrieved` names.
        - A scalar or `Procedural` parameter reads `text` and is rewritten
          whole; a `Procedural` one through a post-condition that re-parses the
          result, so a gradient can never leave unparseable code in the store.
        """
        del kwargs
        texts = [g.text for g in feedback]

        if self._is_numeric_field(name):
            scores = [g.score for g in feedback if g.score is not None]
            if not scores:
                # No score channel means no evidence about this value. Rewriting
                # it from the text alone would be invention, and a loop cannot
                # tell invention from learning.
                return
            score = min(max(sum(scores) / len(scores), 0.0), 1.0)
            current = self.numeric_value(name)
            self._record_observation(name, current, score, " | ".join(texts))
            proposal = self._numeric_update(name, current, score)
            annotation = self._resolve_field(name).annotation
            self._write_scalar(name, _format_numeric(annotation, proposal))
            self.connection.commit()
            return

        if self._is_procedural(name):
            updated = self._rewrite_code_fn.run_sync(
                value=self._read_scalar(name),
                feedback=texts,
                description=self._get_description(name),
            )
            self._write_scalar(name, updated)
            self.connection.commit()
            return

        if self._is_list_field(name):
            self._consolidate_entries(name, texts, retrieved)
            return

        updated = self._rewrite_value_fn.run_sync(
            value=self._read_scalar(name),
            feedback=texts,
            description=self._get_description(name),
        )
        self._write_scalar(name, updated)
        self.connection.commit()

    def _consolidate_entries(
        self, name: str, feedback: list[str], retrieved: dict[str, str] | None
    ) -> None:
        """Agentic entry editing, scoped to the entries the forward pass retrieved.

        Values are re-read from the store rather than taken from `retrieved`,
        because an entry may have been rewritten by an earlier consolidation in
        the same round and the agent must edit what is there now. Ids that no
        longer resolve are dropped. With no usable retrieval context the full
        entry set is shown, which is the honest fallback: a gradient whose
        forward pass we cannot localize should not pretend to be narrow.
        """
        from ai_functions.optimizer._formatting import to_yaml

        entries = self.list_entries(name)
        scoped = {i: entries[i] for i in (retrieved or {}) if i in entries} or entries
        fn = self._edit_entries_fn.replace(tools=[EntryToolProvider(self, name)])
        fn.run_sync(
            retrieved=to_yaml(scoped),
            feedback=_bullets(feedback),
            description=self._get_description(name),
        )
        self.connection.commit()

    def _delete(self, name: str) -> None:
        """Reset a parameter to its schema default."""
        field_info = self._resolve_field(name)
        if field_info.is_required():
            raise ValueError(
                f"Cannot delete required parameter '{name}': it has no schema default."
            )
        default = field_info.get_default(call_default_factory=True)
        self._save(name, default)
        if self._is_numeric_field(name):
            self.connection.execute(
                "DELETE FROM memory_score_observation WHERE actor_id = ? AND param = ?",
                (self.actor_id, name),
            )
            self.connection.commit()

    # ── Tool provider ──

    def tool_provider(self, *names: str, operations: set[str] | None = None) -> DynamicToolProvider:
        """Extend the base tools with entry-id CRUD for list parameters.

        Mirrors `JSONMemoryBackend`, so an agent written against that backend's
        tool names keeps working: `add_to_<name>`, `update_<name>`,
        `delete_from_<name>` on top of the base `recall_` / `query_` /
        `search_` / `save_` / `delete_`.
        """
        ops = operations or {"recall", "query", "search", "save", "delete", "add", "update"}
        provider = super().tool_provider(*names, operations=ops)
        extra: list[AgentTool] = []
        for name in names:
            if not self._is_list_field(name):
                continue
            description = self._get_description(name) or name
            safe = name.replace("/", "_")
            if "add" in ops:
                extra.append(
                    _strands_tool(
                        name=f"add_to_{safe}", description=f"Add a new entry to: {description}"
                    )(self._entry_add_tool(name))
                )
            if "update" in ops:
                extra.append(
                    _strands_tool(
                        name=f"update_{safe}",
                        description=f"Update an entry by entry_id in: {description}",
                    )(self._entry_update_tool(name))
                )
            if "delete" in ops:
                extra.append(
                    _strands_tool(
                        name=f"delete_from_{safe}",
                        description=f"Delete an entry by entry_id from: {description}",
                    )(self._entry_delete_tool(name))
                )
        return DynamicToolProvider(provider.tools + extra)

    def _entry_add_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _add(value: str) -> str:
            """Add a new entry to this list.

            Args:
                value: The text content of the new entry.
            """
            return f"Added with entry_id={self.add_entry(name, value)}"

        return _add

    def _entry_update_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _update(entry_id: str, value: str) -> str:
            """Update an existing entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to update.
                value: The new text content.
            """
            if not self.update_entry(name, entry_id, value):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Updated entry_id={entry_id}"

        return _update

    def _entry_delete_tool(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
        def _delete(entry_id: str) -> str:
            """Delete an entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to delete.
            """
            if not self.remove_entry(name, entry_id):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Deleted entry_id={entry_id}"

        return _delete

    def __str__(self) -> str:
        """Human-readable dump of every parameter, for a report or a log."""
        lines: list[str] = []
        for name in self._leaf_parameter_names():
            if self._is_list_field(name):
                lines.append(f"{name}:")
                lines += [f"  [{i}] {v}" for i, v in self._entry_rows(name)]
            else:
                lines.append(f"{name}: {self._read_scalar(name)}")
        return "\n".join(lines)


_EXPLORE_DECAY = 0.6
"""Trust-region shrink per numeric observation. Makes the explore step converge."""

_EXCLUSIVE_EPSILON = 1e-6
"""How far inside an exclusive (`gt` / `lt`) float bound the search may propose.

An open interval has no last point, so a search over one must stop somewhere short
of the endpoint. Naming the distance here is better than letting a proposal of
exactly the endpoint fail Pydantic validation several layers away from its cause.
"""

_TRUST_FRACTION = 0.25
"""Largest first explore step, as a fraction of the declared domain.

Without a cap, a badly-scored value proposes a jump to the far boundary: on
`Field(20, ge=1, le=100)` a score of 0.2 proposed 99. That is not a search over
the domain, it is one sample at each end, and a loop doing it would report
movement while measuring almost nothing.
"""


class EntryToolProvider(ToolProvider):
    """Entry CRUD scoped to one list parameter, for the consolidation agent.

    Handed to the agentic consolidator so it edits by `entry_id` rather than
    rewriting the list. That is what keeps an untouched entry byte-identical: a
    whole-list rewrite paraphrases everything it was not asked about, and the
    next round's retrieval then works over text nobody chose.
    """

    def __init__(self, backend: TursoMemoryBackend, name: str) -> None:
        self._backend = backend
        self._name = name
        self._consumers: set[object] = set()
        self._tools: list[AgentTool] = self._build_tools()

    def _build_tools(self) -> list[AgentTool]:
        backend, name = self._backend, self._name

        def search_entries(query: str, k: int = 5) -> list[dict[str, str]]:
            """Search entries by semantic relevance to a query.

            Args:
                query: A phrase describing what to look for.
                k: Maximum number of results.

            Returns:
                `{"entry_id", "value", "distance"}` dicts, closest first.
            """
            return [
                {"entry_id": h.entry_id, "value": h.value, "distance": f"{h.distance:.4f}"}
                for h in backend.search_entries(name, query, k)
            ]

        def add_entry(value: str) -> str:
            """Add a new entry to this list.

            Args:
                value: The text content of the new entry.
            """
            return f"Added with entry_id={backend.add_entry(name, value)}"

        def update_entry(entry_id: str, value: str) -> str:
            """Update an existing entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to update.
                value: The new text content.
            """
            if not backend.update_entry(name, entry_id, value):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Updated entry_id={entry_id}"

        def delete_entry(entry_id: str) -> str:
            """Delete an entry by its stable entry_id.

            Args:
                entry_id: The stable identifier of the entry to delete.
            """
            if not backend.remove_entry(name, entry_id):
                raise ValueError(f"entry_id={entry_id} not found")
            return f"Deleted entry_id={entry_id}"

        return [
            _strands_tool(name="search_entries", description="Search entries by relevance.")(
                search_entries
            ),
            _strands_tool(name="add_entry", description="Add a new entry.")(add_entry),
            _strands_tool(name="update_entry", description="Update an entry by entry_id.")(
                update_entry
            ),
            _strands_tool(name="delete_entry", description="Delete an entry by entry_id.")(
                delete_entry
            ),
        ]

    async def load_tools(self, **kwargs: object) -> Sequence[AgentTool]:
        """Return the entry CRUD tools."""
        return self._tools

    def add_consumer(self, consumer_id: object, **kwargs: object) -> None:
        """Register a consumer (bookkeeping only)."""
        self._consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: object, **kwargs: object) -> None:
        """Deregister a consumer (bookkeeping only)."""
        self._consumers.discard(consumer_id)


# ── Helpers ──


def connect(path: Path | str) -> turso.Connection:
    """Open a Turso database in WAL mode, matching `casestudy.eventlog.connect`.

    WAL for the same practical reason it is set there: a training loop reads
    parameters while the interpreter writes run traces to the same file, and WAL
    lets the readers proceed without blocking the writer. Verified against
    `pyturso` 0.7.2 — `PRAGMA journal_mode=WAL` returns `('wal',)` and a
    `-wal` file appears next to the database.
    """
    connection = turso.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _nested_attr(obj: Any, path: str) -> Any:  # pyright: ignore[reportExplicitAny]
    for part in path.split("/"):
        obj = getattr(obj, part)
    return obj


def _numeric_type(annotation: Any) -> type[int] | type[float] | None:  # pyright: ignore[reportExplicitAny]
    """Return `int`/`float` when `annotation` is that, unwrapping wrappers.

    `bool` is excluded even though it subclasses `int`: a flag is not a
    quantity, and a hill climb over `{0, 1}` would be nonsense.
    """
    if annotation is bool:
        return None
    if annotation is int or annotation is float:
        return annotation
    if get_origin(annotation) is None:
        return None
    for argument in get_args(annotation):
        found = _numeric_type(argument)
        if found is not None:
            return found
    return None


def _format_numeric(annotation: Any, value: float) -> str:  # pyright: ignore[reportExplicitAny]
    """Render a numeric parameter for storage, keeping ints integral."""
    return str(int(round(value))) if _numeric_type(annotation) is int else repr(float(value))
