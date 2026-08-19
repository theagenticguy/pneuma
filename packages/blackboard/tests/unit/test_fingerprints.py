"""Property tests for the binding fingerprint (handoff §11).

The gate and the reviews/approvals tables must agree on one binding identity. These
Hypothesis properties pin the three guarantees the unified fingerprint depends on:
order-independence, the single-binding equivalence, and content-hash sensitivity.
"""

from __future__ import annotations

from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from sdlc_blackboard.domain.common import ArtifactBinding
from sdlc_blackboard.domain.reviews import (
    binding_fingerprint,
    single_binding_fingerprint,
)

_uuids = st.uuids(version=4)
_names = st.text(min_size=1, max_size=64)
_hashes = st.text(min_size=1, max_size=64)


@st.composite
def _bindings(draw: st.DrawFn) -> ArtifactBinding:
    return ArtifactBinding(
        artifact_id=draw(_uuids),
        revision_id=draw(_uuids),
        logical_name=draw(_names),
        content_hash=draw(_hashes),
    )


@given(st.lists(_bindings(), min_size=1, max_size=6))
def test_binding_fingerprint_is_order_independent(bindings: list[ArtifactBinding]) -> None:
    forward = tuple(bindings)
    reverse = tuple(reversed(bindings))
    assert binding_fingerprint(forward) == binding_fingerprint(reverse)


@given(_bindings())
def test_single_binding_matches_tuple_of_one(binding: ArtifactBinding) -> None:
    assert single_binding_fingerprint(binding) == binding_fingerprint((binding,))


@given(_bindings(), _hashes)
def test_differing_content_hash_changes_fingerprint(
    binding: ArtifactBinding, other_hash: str
) -> None:
    if other_hash == binding.content_hash:
        return
    changed = binding.model_copy(update={"content_hash": other_hash})
    assert single_binding_fingerprint(binding) != single_binding_fingerprint(changed)


@given(_uuids, _uuids, _uuids)
def test_differing_revision_changes_fingerprint(a: UUID, r1: UUID, r2: UUID) -> None:
    if r1 == r2:
        return
    b1 = ArtifactBinding(artifact_id=a, revision_id=r1, logical_name="x", content_hash="h")
    b2 = ArtifactBinding(artifact_id=a, revision_id=r2, logical_name="x", content_hash="h")
    assert single_binding_fingerprint(b1) != single_binding_fingerprint(b2)
