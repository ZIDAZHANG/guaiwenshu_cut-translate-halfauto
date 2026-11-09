#!/usr/bin/env python3
"""Stage implementation registry for the v2 runner.

W03 registers INPUT, MEDIA_PREP, OCR and ASR. SOURCE, TRANSLATION and SUBTITLE_BUILD arrive with W04 and
QUALITY_AND_COMMIT with W06; until then a request stops with STAGE_UNAVAILABLE at the first stage that has no
implementation instead of pretending to run.
"""

from __future__ import annotations

from upgrade.runner import StageImpl


def default_registry() -> dict[str, StageImpl]:
    """Return the stage implementations available in this build."""
    from upgrade.stages import AsrStage, InputStage, MediaPrepStage, OcrStage
    return {stage.name: stage for stage in (InputStage(), MediaPrepStage(), OcrStage(), AsrStage())}


__all__ = ["default_registry"]
