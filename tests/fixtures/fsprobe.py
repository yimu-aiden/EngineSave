"""What the drive of the temporary folders can do (D62 ④): a few tests depend on file times
kept to the millisecond and on hard links. They are probed once, on that drive -- since
0.11.0 (D66 11) the drive of pytest's ``--basetemp`` (or ``PYTEST_DEBUG_TEMPROOT``), which
conftest names in ``ENGINESAVE_FSPROBE_DIR``, else the system's TEMP; the tests that need
them are skipped, with the reason, where the drive cannot."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import pytest

PROBE_DIR_ENV = "ENGINESAVE_FSPROBE_DIR"


def _probe() -> tuple[bool, bool]:
    where = os.environ.get(PROBE_DIR_ENV) or None
    with tempfile.TemporaryDirectory(prefix="enginesave-fsprobe-", dir=where) as folder:
        probe = Path(folder) / "probe"
        probe.write_bytes(b"x")
        stamp = 1_700_000_000_123_000_000  # .123 seconds
        milliseconds = False
        with contextlib.suppress(OSError):
            os.utime(probe, ns=(stamp, stamp))
            milliseconds = probe.stat().st_mtime_ns // 1_000_000 == stamp // 1_000_000
        links = False
        with contextlib.suppress(OSError, NotImplementedError):
            os.link(probe, Path(folder) / "link")
            links = probe.stat().st_nlink == 2
        return milliseconds, links


MILLISECOND_TIMES, HARD_LINKS = _probe()

needs_millisecond_times = pytest.mark.skipif(
    not MILLISECOND_TIMES, reason="the temporary drive does not keep times to the millisecond"
)
needs_hard_links = pytest.mark.skipif(
    not HARD_LINKS, reason="the temporary drive has no hard links"
)
