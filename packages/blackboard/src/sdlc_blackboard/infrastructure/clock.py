"""System clock adapter implementing the Clock port."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Real wall-clock time source (Clock port)."""

    def now(self) -> datetime:
        return datetime.now(UTC)
