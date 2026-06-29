"""
Shim: settings page preview lives in ``testingEnvironments/settings_page_test.py``.

Run::

    python -m pigeon.settings_page_test

or::

    python testingEnvironments/settings_page_test.py
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    target = (
        Path(__file__).resolve().parents[3]
        / "testingEnvironments"
        / "settings_page_test.py"
    )
    if not target.is_file():
        sys.stderr.write(f"pigeon: missing {target}\n")
        return 1
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
