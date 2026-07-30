"""Convert a `Process` IR to a Petri net so standard tools can score it.

Kept separate from `benchmark.py` because the conversion is useful on its own: any
pm4py conformance or visualisation routine will accept the result.

The mapping is one-to-one, which is only possible because the IR is deliberately
simple. Each state becomes a place, each transition becomes a Petri transition
labelled with the activity it arrives at, and the terminal states form the final
marking. No silent transitions are introduced — the baseline miners need dozens of
them, and every one is a construct with no counterpart in the business process.
"""

from __future__ import annotations

from typing import Any

from ..process.ir import Process


def ir_to_petri(process: Process) -> tuple[Any, Any, Any]:
    """Return `(net, initial_marking, final_marking)` for `process`.

    Raises:
        ImportError: pm4py is not installed (it is a dev-only dependency).
    """
    from pm4py.objects.petri_net.obj import Marking, PetriNet
    from pm4py.objects.petri_net.utils import petri_utils

    net = PetriNet(process.name)
    places = {}
    for state in process.states:
        place = PetriNet.Place(state.name)
        net.places.add(place)
        places[state.name] = place

    labels = {state.name: state.description or state.name for state in process.states}
    for transition in process.transitions:
        # The label is the activity being *entered*: our states are activities, so a
        # transition firing corresponds to that activity occurring in the log.
        petri_transition = PetriNet.Transition(transition.name, labels[transition.target])
        net.transitions.add(petri_transition)
        petri_utils.add_arc_from_to(places[transition.source], petri_transition, net)
        petri_utils.add_arc_from_to(petri_transition, places[transition.target], net)

    initial = Marking({places[process.initial_state]: 1})
    final = Marking({places[s.name]: 1 for s in process.states if s.terminal})
    return net, initial, final
