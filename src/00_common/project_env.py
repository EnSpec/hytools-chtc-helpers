"""Bootstrap conventions shared by every layer script, plus the NEON token lookup.

Python cannot import a package whose name starts with a digit, so every script under
src/<layer>/ starts with the same two lines::

    import sys; from pathlib import Path
    sys.path[:0] = [str(Path(__file__).resolve().parents[2]),
                    str(Path(__file__).resolve().parents[1] / "00_common")]

after which ``from config.paths import ...`` and ``import neon_api`` both work, on this
machine and inside a CHTC job (where the job wrapper unpacks the same tree).
"""

from __future__ import annotations

import os


def neon_token() -> str | None:
    """NEON API token: env var NEON_API_TOKEN first (CHTC jobs), else config/credentials.py."""
    token = os.environ.get("NEON_API_TOKEN")
    if token:
        return token
    try:
        from config.credentials import NEON_API_TOKEN

        return NEON_API_TOKEN or None
    except ImportError:
        return None
