"""
Shim: mic EQ visualizer test lives in ``testingEnvironments/mic_viz_test.py``.

Run::

    python -m pigeon.mic_viz_test

or::

    python testingEnvironments/mic_viz_test.py
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    target = Path(__file__).resolve().parents[1] / "testingEnvironments" / "mic_viz_test.py"
    if not target.is_file():
        sys.stderr.write(f"pigeon: missing {target}\n")
        return 1
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
