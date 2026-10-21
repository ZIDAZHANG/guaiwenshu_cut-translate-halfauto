#!/usr/bin/env python3
"""Regression tests for deterministic Jianying executable selection."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from jianying_uia_probe import executable_from_config, same_installation


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jianying_selection_") as temp_dir:
        root = Path(temp_dir)
        configured = root / "portable" / "JianyingPro.exe"
        configured.parent.mkdir()
        configured.write_bytes(b"launcher")
        config = root / "config.json"
        config.write_text(
            json.dumps({"local": {"jianying_exe": str(configured)}}),
            encoding="utf-8",
        )

        assert executable_from_config(config) == configured
        assert same_installation(
            configured,
            root / "portable" / "6.0.1.11779" / "JianyingPro.exe",
        )
        assert not same_installation(
            root / "Apps" / "10.9.0.14199" / "JianyingPro.exe",
            root / "Apps" / "11.2.5.14316" / "JianyingPro.exe",
        )
        assert not same_installation(
            root / "portable" / "JianyingPro.exe",
            root / "another" / "6.0.1.11779" / "JianyingPro.exe",
        )

    print("Jianying UI selection tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
