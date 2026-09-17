#!/usr/bin/env python3
"""List PIDs of real `run.py verify` python workers.

Why this exists: `pgrep -f "run.py verify"` also matches the *shell* that is
running the guard command itself (its own command line contains the pattern),
which made a naive guard report phantom workers and refuse to start the single
verifier. This scans /proc directly and counts a process only when:

  * argv[0] is a python interpreter, and
  * "run.py" and "verify" appear as real argv tokens.

So the guard can never mistake a bash wrapper, a grep, or itself for a worker.

Usage:
  python3 scripts/verify_pids.py          # one PID per line
  python3 scripts/verify_pids.py --count  # just the number
"""

from __future__ import annotations

import os
import sys


def verifier_pids() -> list[int]:
    me = os.getpid()
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except (OSError, PermissionError):
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if not argv:
            continue
        exe = os.path.basename(argv[0])
        if not exe.startswith("python"):
            continue
        tokens = {os.path.basename(a) for a in argv[1:]}
        if "run.py" in tokens and "verify" in tokens:
            found.append(pid)
    return sorted(found)


if __name__ == "__main__":
    pids = verifier_pids()
    if "--count" in sys.argv:
        print(len(pids))
    else:
        for pid in pids:
            print(pid)
