"""The shadow gate: one environment variable, default OFF.

``SIGNAL_SHADOW=1`` turns the volatility reading into advisory *text*. It does not turn on any
authority, because there is none to turn on. With the flag unset - the default - every code path in
this lane is inert and the surrounding loop behaves exactly as it does without the package installed.
"""

from __future__ import annotations

import os

__all__ = ["SHADOW_ENV", "shadow_enabled"]

SHADOW_ENV = "SIGNAL_SHADOW"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def shadow_enabled(environ: dict[str, str] | None = None) -> bool:
    """True only for an explicit affirmative. Anything else - including unset - is OFF."""
    source = os.environ if environ is None else environ
    raw = source.get(SHADOW_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES
