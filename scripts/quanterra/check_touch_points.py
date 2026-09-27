#!/usr/bin/env python3
"""Fail when a Quanterra change lands outside the paths Quanterra owns.

The fork stays mergeable with upstream Open WebUI because every change is either
inside a Quanterra-owned directory or in one of the few upstream files listed in
``quanterra/touch-points.txt``. This compares the tree with the upstream tag in
``backend/open_webui/quanterra/version.py``.

Usage: ``python scripts/quanterra/check_touch_points.py`` (the upstream tag must
be fetched, e.g. ``git fetch origin tag v0.11.4``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / 'backend' / 'open_webui' / 'quanterra' / 'version.py'
TOUCH_POINTS_FILE = ROOT / 'quanterra' / 'touch-points.txt'
OWNED_PREFIXES = (
    'backend/open_webui/quanterra/',
    'src/lib/components/quanterra/',
    'quanterra/',
    'scripts/quanterra/',
    '.github/workflows/',
)


def upstream_tag() -> str:
    match = re.search(r"^UPSTREAM_TAG = '([^']+)'", VERSION_FILE.read_text(encoding='utf-8'), re.MULTILINE)
    if not match:
        sys.exit(f'UPSTREAM_TAG not found in {VERSION_FILE}')
    return match.group(1)


def touch_points() -> set[str]:
    lines = TOUCH_POINTS_FILE.read_text(encoding='utf-8').splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith('#')}


def changed_paths(tag: str) -> list[str]:
    subprocess.run(
        ['git', 'rev-parse', '--verify', '--quiet', f'{tag}^{{commit}}'], cwd=ROOT, check=True, capture_output=True
    )
    # Index and working tree against the tag, so it also works before a commit.
    output = subprocess.run(
        ['git', 'diff', '--name-only', tag], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return sorted(line for line in output.splitlines() if line)


def main() -> int:
    tag = upstream_tag()
    allowed = touch_points()
    strays = [path for path in changed_paths(tag) if not path.startswith(OWNED_PREFIXES) and path not in allowed]
    if strays:
        print(f'Changes outside the Quanterra-owned paths (relative to upstream {tag}):')
        for path in strays:
            print(f'  {path}')
        print(f'Move the change into a Quanterra directory or list the file in {TOUCH_POINTS_FILE.name}.')
        return 1
    print(f'OK: every change since upstream {tag} is Quanterra-owned or a listed touch point.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
