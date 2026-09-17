"""Regression tests for the verification durability watchdog.

Why these exist
---------------
``scripts/checkpoint_push.sh`` publishes the live verification artefacts to
``origin/main`` *while a verifier process is still appending rows to them*.
The previous implementation did that through the index and the working tree
(``git add`` → ``git commit`` → ``git pull --rebase`` → ``git push``), which is
a genuine data-loss race:

* ``git pull --rebase`` rewrites the working tree. To replay a local commit on
  top of a remote one, git first restores the tree to the remote's content —
  truncating ``candidates_verified.jsonl`` back to the remote's shorter
  version — and then writes the local version back. The verifier holds the
  file open in append mode for the whole window, so rows written during it are
  destroyed. They are unrecoverable, because the runner's resume set was read
  once at start-up and the process never re-verifies them.
* ``git add`` on a file being appended to can snapshot a half-written last
  line, publishing a torn JSON row.

These tests build a throwaway git repository with a real local "remote", run
the real script's publish path against it *with a writer actively appending*,
and assert the two properties that make the watchdog safe:

1. **the working tree is never rewritten** — the on-disk artefact only ever
   grows while the watchdog is publishing, even when the remote has moved on;
2. **only complete rows are published** — a torn trailing line is never
   committed, and the published prefix is always a prefix of the real file.

Everything runs locally against ``file://`` remotes; no network access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "checkpoint_push.sh"
VERIFIED = "data/interim/candidates_verified.jsonl"
STATE = "data/interim/candidates_verification_state.json"
REPORT = "data/interim/candidates_verification_report.json"


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for the watchdog tests"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


def _row(index: int) -> str:
    return json.dumps({"candidate_id": f"c{index}", "name": f"tool {index}"}) + "\n"


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A work repo with a real (bare) origin, seeded with 2 verified rows."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(origin))

    interim = work / "data" / "interim"
    interim.mkdir(parents=True)
    (interim / "candidates_verified.jsonl").write_text(_row(0) + _row(1), encoding="utf-8")
    (interim / "candidates_verification_state.json").write_text(
        json.dumps({"records_persisted": 2}), encoding="utf-8"
    )
    (interim / "candidates_verification_report.json").write_text(
        json.dumps({"input": 2}), encoding="utf-8"
    )
    # A second, unrelated tracked file: the remote will change it, and the
    # watchdog must carry that change forward rather than revert it.
    (work / "OTHER.md").write_text("base\n", encoding="utf-8")

    _git(work, "add", "-A")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "-q", "origin", "main")
    return work, origin


def _install_script(work: Path) -> Path:
    """Copy the real script in, retargeted at the throwaway repo."""
    scripts = work / "scripts"
    scripts.mkdir(exist_ok=True)
    body = SCRIPT.read_text(encoding="utf-8").replace(
        "cd /home/user/webapp || exit 1", f"cd {work} || exit 1"
    )
    target = scripts / "checkpoint_push.sh"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)

    # The worker probe is copied verbatim: the publish path calls it only
    # through `workers()`, which these tests do not exercise.
    shutil.copy(PROJECT_ROOT / "scripts" / "verify_pids.py", scripts / "verify_pids.py")
    return target


def _publish(work: Path, script: Path, count: int) -> subprocess.CompletedProcess[str]:
    """Run only the script's publish() function, once, like the loop does."""
    driver = (
        f"set -uo pipefail\n"
        f'source "{script}" >/dev/null 2>&1 || true\n'
    )
    # `source` would run the infinite loop, so instead extract the function
    # body by running the script with the loop disabled via an env guard.
    # Simpler and more honest: invoke the script with a 1s interval and kill
    # it after the first publish. We do that in the callers instead.
    raise AssertionError(driver)  # pragma: no cover - not used


def _run_watchdog_once(work: Path, script: Path, *, timeout: float = 60.0) -> str:
    """Run the watchdog with a short interval until it publishes once.

    The loop exits on its own as soon as no verifier worker is running, which
    is always true in the test environment, so a single cycle is enough.
    """
    result = subprocess.run(
        ["bash", str(script), "1"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout + result.stderr


def _published_rows(work: Path) -> list[str]:
    out = subprocess.run(
        ["git", "cat-file", "-p", f"refs/heads/main:{VERIFIED}"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# The race itself
# --------------------------------------------------------------------------
def test_publishing_never_shrinks_the_artefact_while_a_writer_appends(tmp_path: Path) -> None:
    """The on-disk artefact must only ever grow while the watchdog publishes.

    This is the exact failure the old ``git pull --rebase`` path produced: the
    tree was reset to the remote's shorter file mid-append. The writer thread
    here plays the verifier, and a sampler watches the file's size for any
    decrease. A single decrease means rows were destroyed.
    """
    work, origin = _make_repo(tmp_path)
    script = _install_script(work)
    artefact = work / VERIFIED

    # Make the remote move ahead, so the watchdog MUST integrate remote work.
    # Under the old implementation this is precisely what triggered the
    # rebase that rewrote the working tree.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "OTHER.md").write_text("remote moved on\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote work")
    _git(other, "push", "-q", "origin", "main")

    stop = threading.Event()
    shrank: list[tuple[int, int]] = []

    def writer() -> None:
        index = 2
        with artefact.open("a", encoding="utf-8") as handle:
            while not stop.is_set():
                handle.write(_row(index))
                handle.flush()
                index += 1
                time.sleep(0.01)

    def sampler() -> None:
        last = artefact.stat().st_size
        while not stop.is_set():
            try:
                size = artefact.stat().st_size
            except FileNotFoundError:
                shrank.append((last, -1))
                return
            if size < last:
                shrank.append((last, size))
            last = size
            time.sleep(0.005)

    threads = [threading.Thread(target=writer), threading.Thread(target=sampler)]
    for thread in threads:
        thread.daemon = True
        thread.start()
    time.sleep(0.3)

    try:
        _run_watchdog_once(work, script)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    assert not shrank, (
        "the verification artefact was truncated while the watchdog published: "
        f"{shrank[:3]} — rows appended in that window are lost forever"
    )


def test_publish_carries_remote_changes_forward_without_reverting_them(tmp_path: Path) -> None:
    """A newer remote commit must survive the checkpoint, not be reverted.

    Building the commit on top of ``origin/main`` replaces ``pull --rebase``;
    this asserts it has the same *outcome* (remote work preserved) without the
    tree write.
    """
    work, origin = _make_repo(tmp_path)
    script = _install_script(work)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "OTHER.md").write_text("remote moved on\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote work")
    _git(other, "push", "-q", "origin", "main")

    with (work / VERIFIED).open("a", encoding="utf-8") as handle:
        handle.write(_row(2))

    _run_watchdog_once(work, script)

    published = subprocess.run(
        ["git", "cat-file", "-p", "refs/heads/main:OTHER.md"],
        cwd=work,
        capture_output=True,
        text=True,
    ).stdout
    assert published.strip() == "remote moved on", (
        "the checkpoint reverted a newer remote commit"
    )
    assert len(_published_rows(work)) == 3


def test_a_torn_trailing_row_is_never_published(tmp_path: Path) -> None:
    """A half-written final line must be excluded, not committed."""
    work, _origin = _make_repo(tmp_path)
    script = _install_script(work)

    with (work / VERIFIED).open("a", encoding="utf-8") as handle:
        handle.write(_row(2))
        handle.write('{"candidate_id": "c3", "na')  # torn mid-append

    _run_watchdog_once(work, script)

    rows = _published_rows(work)
    assert len(rows) == 3, rows
    for row in rows:
        json.loads(row)  # every published row parses


def test_published_rows_are_a_prefix_of_the_real_file(tmp_path: Path) -> None:
    """The snapshot may lag the file, but must never diverge from it."""
    work, _origin = _make_repo(tmp_path)
    script = _install_script(work)

    with (work / VERIFIED).open("a", encoding="utf-8") as handle:
        for index in range(2, 12):
            handle.write(_row(index))

    _run_watchdog_once(work, script)

    on_disk = [
        line
        for line in (work / VERIFIED).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    published = _published_rows(work)
    assert published == on_disk[: len(published)]
    assert len(published) == len(on_disk)


def test_publish_refuses_to_shrink_the_published_dataset(tmp_path: Path) -> None:
    """A shorter local artefact must never overwrite a longer published one."""
    work, origin = _make_repo(tmp_path)
    script = _install_script(work)

    # Remote already has 5 rows.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    (other / VERIFIED).write_text("".join(_row(i) for i in range(5)), encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote checkpoint with 5 rows")
    _git(other, "push", "-q", "origin", "main")

    # Locally we only have 3.
    (work / VERIFIED).write_text("".join(_row(i) for i in range(3)), encoding="utf-8")

    _run_watchdog_once(work, script)

    assert len(_published_rows(work)) == 5, (
        "a shorter local artefact was published over a longer remote one"
    )


def test_publish_leaves_the_real_index_untouched(tmp_path: Path) -> None:
    """Publishing must not stage anything in the developer's index."""
    work, _origin = _make_repo(tmp_path)
    script = _install_script(work)

    with (work / VERIFIED).open("a", encoding="utf-8") as handle:
        handle.write(_row(2))
    # An unrelated, deliberately uncommitted edit that must stay unstaged.
    (work / "OTHER.md").write_text("local scratch\n", encoding="utf-8")

    _run_watchdog_once(work, script)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=work,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert staged == "", f"the watchdog staged files in the real index: {staged}"
    assert (work / "OTHER.md").read_text(encoding="utf-8") == "local scratch\n"
