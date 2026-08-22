"""ADR-35 acceptance (#172) — run INSIDE the runner image, with §7.2's flags.

Every existing host test passed while this defect was live, because on the dev
host the runner and the CLI share a uid: `os.kill` succeeds and the EPERM that
production hits never happens. So these items run in a container built from
`docker/session-runner`, as pid 1 root, with the exact flags `supervisor.py`
passes — and item (1) proves the fixture *is* the environment the bug lives in
before asserting the remedy.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_PROBE = Path(__file__).parent / "probes" / "adr35_in_image.py"

# §7.2, as `_create_container` passes them (supervisor.py:448-461).
_FLAGS = [
    "--pids-limit", "1024",
    "--cap-drop", "ALL",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
    "--security-opt", "no-new-privileges",
]


@pytest.fixture(scope="module")
def probe(session_runner_image: str) -> dict:
    """One container run; each item asserts on its own key."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python3", *_FLAGS,
         "-v", f"{_PROBE}:/probe.py:ro", session_runner_image, "/probe.py"],
        capture_output=True, text=True, check=False, timeout=180,
    )
    if r.returncode != 0:
        pytest.fail(f"in-image probe failed:\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}")
    line = [x for x in r.stdout.splitlines() if x.startswith("{")]
    assert line, f"probe emitted no JSON:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    return json.loads(line[-1])


def test_1_fixture_is_the_environment_then_the_helper_kills(probe: dict) -> None:
    """(1) Both legs. The EPERM leg is what makes the remedy leg mean anything."""
    assert probe["1_pid1_is_root"], "pid 1 must be root for this to be the real case"
    assert probe["1_capeff"] == "00000000000000c9", probe["1_capeff"]
    assert int(probe["1_capeff"], 16) & (1 << 5) == 0, "CAP_KILL (bit 5) must be absent"
    # the defect, reproduced
    assert probe["1_root_kill_eperm"], "root os.kill of a uid-1001 child must raise EPERM"
    assert probe["1_child_survived_root_kill"], "and the child must survive it"
    # the remedy
    assert probe["1_kill_reason"] is None, probe["1_kill_reason"]
    assert probe["1_proc_cleared"], "state is cleared only once the process is dead"


def test_2_no_descendant_survives_the_kill(probe: dict) -> None:
    """(2) Asserted by State:, never by the existence of /proc/<pid>."""
    assert probe["2_descendants_before"] >= 1, "fixture must have descendants to kill"
    assert probe["2_none_alive"], probe["2_descendant_states"]


def test_3_spawn_puts_the_cli_in_its_own_process_group(probe: dict) -> None:
    """(3) Asserted independently of any kill — without it killpg is killpg(1)."""
    assert probe["3_pgid_equals_pid"]
    assert probe["3_pgid_is_not_runners"]


def test_4_failed_kill_is_visible_and_spawns_no_second_cli(probe: dict) -> None:
    """(4) The leak: clearing state before the kill let a second CLI start."""
    assert probe["4_reason"] == "forced failure"
    assert probe["4_proc_still_set"]
    assert probe["4_is_alive"]
    assert probe["4_process_still_running"], "fixture must keep the cli alive"
    assert probe["4_no_second_cli"]


def test_5_teardown_reports_a_failed_kill_instead_of_success(probe: dict) -> None:
    """(5) FR-4.3 is a claim about the container, so teardown must not lie."""
    assert probe["5_shutdown_all_ok"] == []
    assert probe["5_shutdown_all_failed"] == ["developer: forced failure"]


def test_6_no_untracked_zombies_after_the_sequence(probe: dict) -> None:
    """(6) Scoped to untracked: the direct child is legitimately Z until reaped."""
    assert probe["6_untracked_zombies"] == [], probe["6_untracked_zombies"]


def test_7_the_reaper_does_not_steal_the_siblings_status(probe: dict) -> None:
    """(7) The item that fails a blanket waitpid(-1) — which reports 0, not an error."""
    assert probe["7_sibling_is_unreaped_zombie"], "fixture must make the steal possible"
    assert probe["7_sibling_status_intact"] == probe["7_expected"] == 7


def test_7b_the_reaper_reaches_orphans_past_a_tracked_zombie(probe: dict) -> None:
    """The spin the ADR's first reaper design would have shipped.

    `waitid(..., WNOWAIT)` does not consume, so a peek returns the same child
    every time. With a tracked, reapable, unpolled CLI in front — the state
    right after a kill — the untracked orphans behind it are never reached and
    the reaper is a silent no-op. Asserted here rather than in (6), because
    (6)'s sequence has already reaped the direct child and so cannot produce
    the condition. Without this item the rejected design passes the suite.
    """
    assert probe["7b_had_tracked_zombie_in_front"], "fixture must create the spin"
    assert probe["7b_orphans_created"] >= 1, "fixture must leave orphans to reap"
    assert probe["7b_untracked_zombies_left"] == [], probe["7b_untracked_zombies_left"]
