#!/usr/bin/env python3
"""Stage implementation registry for the v2 runner.

Work packages W03 (INPUT, MEDIA_PREP, OCR, ASR), W04 (SOURCE, TRANSLATION, SUBTITLE_BUILD) and W06
(QUALITY_AND_COMMIT) register their implementations here. Until then the default registry is empty and a
request fails with STAGE_UNAVAILABLE at the first unimplemented stage instead of pretending to run.
"""

from __future__ import annotations

from upgrade.runner import StageImpl


def default_registry() -> dict[str, StageImpl]:
    """Return the stage implementations available in this build (W02: none)."""
    return {}


__all__ = ["default_registry"]
