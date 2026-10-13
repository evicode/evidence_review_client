"""Entry point: ``python -m evidence_review`` and the installed launcher.

Imports here are absolute on purpose. PyInstaller executes this file as a
top-level script rather than as a package submodule, so a relative import
(``from .app import run``) raises "attempted relative import with no known
parent package" in the frozen build while working fine from source.
"""

from __future__ import annotations

import sys


def main() -> int:
    from evidence_review.app import run

    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
