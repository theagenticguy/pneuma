"""Score our mined model against the standard miners, on the same log and metrics.

Without this, "replays 89.8% of cases" is a number floating in space. Process mining
has two accepted measures and they pull against each other:

- **fitness** — can the model replay the log? A model permitting everything scores 1.0.
- **precision** — does the model permit *only* what was observed? A model allowing
  any order scores near 0.

F-score is their harmonic mean, and it is the number to compare on. A third column
matters more than either for our purpose: **soundness**. An unsound model can
deadlock, so it cannot be deployed as a workflow no matter how well it scores.

`ir_to_petri` is what makes the comparison honest: our IR is converted to a Petri
net and handed to pm4py's own evaluators, so every row in the table is measured by
the same code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ir_petri import ir_to_petri
from .miner import mine


@dataclass(frozen=True)
class Score:
    """One model's standing on the accepted metrics."""

    model: str
    transitions: int
    fitness: float
    precision: float
    verifiable: bool
    note: str = ""

    @property
    def f_score(self) -> float:
        total = self.fitness + self.precision
        return 0.0 if not total else round(2 * self.fitness * self.precision / total, 3)


def evaluate(log_path: Path, *, thresholds: tuple[int, ...] = (5, 25, 100)) -> list[Score]:
    """Score every miner on `log_path`.

    Imports pm4py lazily: it is a dev-only dependency and AGPL, so nothing in the
    library's runtime path should require it.
    """
    import pm4py

    from . import eventlog

    log = pm4py.read_xes(str(log_path))
    events = eventlog.parse_xes(log_path)
    scores: list[Score] = []

    baselines = [
        ("pm4py Inductive Miner", lambda: pm4py.discover_petri_net_inductive(log)),
        (
            "pm4py IM infrequent (20%)",
            lambda: pm4py.discover_petri_net_inductive(log, noise_threshold=0.2),
        ),
        ("pm4py Heuristics Miner", lambda: pm4py.discover_petri_net_heuristics(log)),
        ("pm4py Alpha Miner", lambda: pm4py.discover_petri_net_alpha(log)),
    ]

    for label, discover in baselines:
        net, initial, final = discover()
        fitness = pm4py.fitness_token_based_replay(log, net, initial, final)["log_fitness"]
        precision = pm4py.precision_token_based_replay(log, net, initial, final)
        silent = sum(1 for t in net.transitions if t.label is None)
        scores.append(
            Score(
                model=label,
                transitions=len(net.transitions),
                fitness=round(fitness, 3),
                precision=round(precision, 3),
                verifiable=False,
                note=f"{silent} silent transitions",
            )
        )

    for threshold in thresholds:
        discovery = mine(events, name=f"Ours{threshold}", min_edge_cases=threshold)
        net, initial, final = ir_to_petri(discovery.process)
        fitness = pm4py.fitness_token_based_replay(log, net, initial, final)["log_fitness"]
        precision = pm4py.precision_token_based_replay(log, net, initial, final)
        scores.append(
            Score(
                model=f"ours (threshold={threshold})",
                transitions=len(net.transitions),
                fitness=round(fitness, 3),
                precision=round(precision, 3),
                verifiable=True,
                note=f"replays {100 * discovery.coverage:.1f}% of whole cases",
            )
        )

    return scores


def table(scores: list[Score]) -> str:
    header = (
        f"{'model':<30} {'trans':>6} {'fitness':>8} {'precision':>10} "
        f"{'F-score':>8} {'verifiable':>11}  notes"
    )
    rows = [header]
    for score in scores:
        rows.append(
            f"{score.model:<30} {score.transitions:>6} {score.fitness:>8.3f} "
            f"{score.precision:>10.3f} {score.f_score:>8.3f} "
            f"{'yes' if score.verifiable else 'no':>11}  {score.note}"
        )
    return "\n".join(rows)


if __name__ == "__main__":  # pragma: no cover - manual benchmark run
    import sys

    print(table(evaluate(Path(sys.argv[1] if len(sys.argv) > 1 else "data/receipt.xes"))))
