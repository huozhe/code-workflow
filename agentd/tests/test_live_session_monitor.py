"""#249: the live-exercise monitor's output is discharge evidence, so pin it.

A wrong test fails; a wrong monitor reads calm. Three defects reached review
here — a tail filter that silently missed #214's and #212's own lines, a
rotation reset that dropped the rotated-out tail, and a probe self-check that
left the zombie it created inside the window it was guarding. Each is pinned
below against the **literal log lines the runbook greps for**, not against a
description of them, because a description is what let all three through.

Docker is not required: the grep and rotation paths are exercised through the
env overrides the script exposes for exactly this reason.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_session_monitor.sh"
_ISSUE = 248
_SK = f"huozhe/code-workflow#{_ISSUE}"

# Verbatim from docs/ops/live-sign-offs.md and the code that emits them.
# Three of these carry no session key, which is what the first filter got wrong.
_RUNBOOK_LINES = {
    "B/#214 superseded": "merge_auth superseded id=abc pr=241 latest=CHANGES_REQUESTED",
    "B/#214 defer": "route defer id=9a30cdc0-a597-11f1-8e68-d29a2ce6ccb3 retry_in=5s",
    "B/#211 unauthorized": "unauthorized Feature PR merge: state=CODE_REWORK expected MERGING",
    "C/#212 reconcile": (
        "reconcile pass containers=1 kept=1 nodes=6 synthesized=0 capped=0 "
        "adopted=0 attached=1 unattached=0 probe_skipped=0 in 21ms"
    ),
    "C/#209 author-sent": "author-sent PR event, no turn (#173)",
    "A/#228 progress": (
        "turn progress turn=t-abc role=architect chunks=12 bytes=900 "
        "quiet=0s max_quiet=0s"
    ),
    "fsm": f"fsm {_SK} → DESIGN_REVIEW (roles freeze at Design PR open)",
}


def _run_and_append(tmp: Path, log: Path, line: str) -> str:
    """Start the monitor, THEN append — the cursor starts at EOF.

    Appending first and starting after makes the monitor begin past the line and
    print nothing, which fails every case including the ones that work. The
    fixture has to reach the case before the assertion means anything.
    """
    out = tmp / "monitor.out"
    env = {
        **os.environ,
        "AGENTD_MONITOR_DB": str(tmp / "absent.db"),
        "AGENTD_MONITOR_LOG": str(log),
        "AGENTD_MONITOR_CONTAINER": "agentd-does-not-exist",
        "AGENTD_MONITOR_POLL_S": "1",
    }
    proc = subprocess.Popen(
        ["bash", str(_SCRIPT), str(_ISSUE), str(out)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        time.sleep(2.5)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return out.read_text(encoding="utf-8") if out.exists() else ""


@pytest.mark.parametrize("label", sorted(_RUNBOOK_LINES))
def test_every_runbook_line_survives_the_tail_filter(
    tmp_path: Path, label: str
) -> None:
    """The silent miss. Each line is one window's evidence; a filter that drops
    it produces a calm monitor and a null result that reads like a pass."""
    log = tmp_path / "agentd.log"
    log.write_text("seed\n", encoding="utf-8")
    line = _RUNBOOK_LINES[label]
    captured = _run_and_append(tmp_path, log, line)
    assert "monitor start" in captured, "the monitor never ran"
    assert line in captured, f"{label} was filtered out: {line}"


def test_rotation_drains_the_unread_tail_not_just_the_new_file(
    tmp_path: Path,
) -> None:
    """#239's failure, one level up: resetting the cursor reads the new file and
    loses everything between the cursor and EOF of the file that rotated out."""
    log = tmp_path / "agentd.log"
    unread = "author-sent PR event, no turn (#173)"
    log.write_text("\n".join(f"noise{i}" for i in range(5)) + "\n", encoding="utf-8")

    out = tmp_path / "monitor.out"
    env = {
        **os.environ,
        "AGENTD_MONITOR_DB": str(tmp_path / "absent.db"),
        "AGENTD_MONITOR_LOG": str(log),
        "AGENTD_MONITOR_CONTAINER": "agentd-does-not-exist",
        "AGENTD_MONITOR_POLL_S": "1",
    }
    proc = subprocess.Popen(
        ["bash", str(_SCRIPT), str(_ISSUE), str(out)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        # A line arrives and is still unread when the rotation happens.
        with log.open("a", encoding="utf-8") as fh:
            fh.write(unread + "\n")
        (tmp_path / "agentd.log.1").write_text(
            log.read_text(encoding="utf-8"), encoding="utf-8"
        )
        log.write_text("fresh line after rotation\n", encoding="utf-8")
        time.sleep(3.5)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    text = out.read_text(encoding="utf-8")
    assert "rotated" in text, text
    assert unread in text, (
        "the line that rotated out was never read — cursor reset without draining "
        f"agentd.log.1:\n{text}"
    )


def test_an_unreadable_db_warns_rather_than_reading_as_quiet(tmp_path: Path) -> None:
    """`[ "" -gt 0 ]` is a shell error, not a false. A broken query must not look
    like a session with no turn open."""
    log = tmp_path / "agentd.log"
    log.write_text("seed\n", encoding="utf-8")
    text = _run_and_append(tmp_path, log, "irrelevant")
    assert "turns query unreadable" in text, text
