#!/usr/bin/env python3
"""Install a plain git pre-commit hook that runs the Mizan secret scan on staged content.

No dependency on the pre-commit framework: the hook is a small POSIX sh script that git
runs with its bundled sh on every platform (including Git for Windows). If you use the
pre-commit framework instead, ``.pre-commit-config.yaml`` wires up the same scan.

Usage:
    python scripts/install_hooks.py            # install (refuses to overwrite a foreign hook)
    python scripts/install_hooks.py --force    # replace a foreign hook (backup kept as pre-commit.bak)
    python scripts/install_hooks.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

MARKER = "mizan-secret-scan-hook v1"

HOOK = f"""#!/bin/sh
# {MARKER}
# Installed by scripts/install_hooks.py. Runs the Mizan secret scan on STAGED content and
# blocks the commit on any finding. To bypass for a deliberately secret-shaped test fixture,
# add an inline "secret-scan: allow" marker on that line or a glob to .secretscan-allow;
# never bypass for a real credential.
set -e
if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v py >/dev/null 2>&1; then PY="py -3"
else echo "pre-commit: python not found on PATH; cannot run scripts/secret_scan.py" >&2; exit 1
fi
cd "$(git rev-parse --show-toplevel)"
exec $PY scripts/secret_scan.py --staged
"""


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"install_hooks: git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def hooks_dir() -> Path:
    """Respect core.hooksPath; fall back to <gitdir>/hooks."""
    raw = _git("rev-parse", "--git-path", "hooks")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(_git("rev-parse", "--show-toplevel")) / path
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="replace an existing hook that is not ours (backup kept)")
    parser.add_argument("--uninstall", action="store_true", help="remove the hook if it is ours")
    args = parser.parse_args(argv)

    target = hooks_dir() / "pre-commit"
    existing = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
    ours = existing is not None and MARKER in existing

    if args.uninstall:
        if ours:
            target.unlink()
            print(f"install_hooks: removed {target}")
            return 0
        print(f"install_hooks: nothing to remove at {target} (not our hook)")
        return 0

    if existing is not None and not ours:
        if "pre-commit" in existing and "PRE_COMMIT" in existing.upper():
            print(
                "install_hooks: a pre-commit *framework* hook is installed; it already runs the scan via "
                ".pre-commit-config.yaml. Nothing to do.",
            )
            return 0
        if not args.force:
            print(
                f"install_hooks: {target} exists and is not ours. Re-run with --force to replace it "
                "(a backup is kept as pre-commit.bak).",
                file=sys.stderr,
            )
            return 1
        backup = target.with_name("pre-commit.bak")
        backup.write_text(existing, encoding="utf-8")
        print(f"install_hooks: backed up the previous hook to {backup}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"install_hooks: installed {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
