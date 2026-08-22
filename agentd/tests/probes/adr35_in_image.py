"""ADR-35 acceptance, executed INSIDE the runner image as pid 1 (root).

Emits one JSON object on stdout. Every item is measured against the real
`cli_session` code from the image — not a re-implementation — because the
defect (root cannot signal a uid-1001 child) cannot exist anywhere else.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/agentd-runner")
from agentd_runner import cli_session as cs

OUT: dict[str, object] = {}
HOLD = "sleep 300 & sleep 300"  # a "CLI" with a descendant of its own


def state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split(None, 2)[1]
    except OSError:
        return "ABSENT"
    return "?"


def descendants_of(pid: int) -> list[int]:
    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        if line.split()[1] == str(pid):
                            out.append(int(e))
                        break
        except OSError:
            continue
    return out


def make_session(role: str, uid: int) -> cs.LiveCliSession:
    base = f"/tmp/adr35/{role}"
    for sub in ("h", "t", "x"):
        os.makedirs(f"{base}/{sub}", exist_ok=True)
        os.chmod(f"{base}/{sub}", 0o777)
    os.chmod("/tmp/adr35", 0o777)
    return cs.LiveCliSession(
        role=role,
        adapter="claude-code",
        uid=uid,
        home=Path(f"{base}/h"),
        tmp=Path(f"{base}/t"),
        xdg=Path(f"{base}/x"),
        spawn_cwd=Path("/tmp"),
    )


def spawn(sess: cs.LiveCliSession) -> None:
    """Drive the real _popen_unlocked; only the argv is ours."""
    sess._popen_unlocked(["sh", "-c", HOLD])
    time.sleep(0.4)


# --- (1) the EPERM leg, then the remedy ------------------------------------
a = make_session("architect", 1001)
spawn(a)
child = a.proc.pid
try:
    os.kill(child, signal.SIGTERM)
    OUT["1_root_kill_eperm"] = False
except PermissionError:
    OUT["1_root_kill_eperm"] = True
OUT["1_child_survived_root_kill"] = a.proc.poll() is None
OUT["1_pid1_is_root"] = os.geteuid() == 0
with open("/proc/1/status", encoding="utf-8") as _fh:
    OUT["1_capeff"] = _fh.read().split("CapEff:")[1].split()[0]

# --- (3) own process group, asserted independently of any kill -------------
OUT["3_pgid_equals_pid"] = os.getpgid(child) == child
OUT["3_pgid_is_not_runners"] = os.getpgid(child) != os.getpgid(0)

kids = descendants_of(child)
OUT["2_descendants_before"] = len(kids)

# --- (1) remedy + (2) descendants dead + (6) no untracked zombies ----------
reason = a._kill_unlocked()
OUT["1_kill_reason"] = reason
OUT["1_proc_cleared"] = a.proc is None
OUT["2_descendant_states"] = [state(k) for k in kids]
OUT["2_none_alive"] = all(state(k) in ("Z", "ABSENT") for k in kids)
OUT["6_untracked_zombies"] = [
    p
    for p in (int(x) for x in os.listdir("/proc") if x.isdigit())
    if state(p) == "Z" and p not in cs._tracked_pids()
]

# --- (4) a failed kill is visible and non-destructive ----------------------
b = make_session("developer", 1002)
spawn(b)
held = b.proc.pid
orig = cs.LiveCliSession._kill_group_unlocked
cs.LiveCliSession._kill_group_unlocked = lambda self, proc: "forced failure"
reason_b = b._kill_unlocked()
OUT["4_reason"] = reason_b
OUT["4_proc_still_set"] = b.proc is not None
OUT["4_is_alive"] = b.is_alive()
OUT["4_process_still_running"] = state(held) not in ("Z", "ABSENT")
spawned = []
cs.LiveCliSession._spawn_unlocked = lambda self, **k: spawned.append(1)
b.ensure_spawned()
OUT["4_no_second_cli"] = spawned == []
cs.LiveCliSession._kill_group_unlocked = orig

# --- (7) the reaper does not steal ----------------------------------------
c = make_session("architect", 1001)
c._popen_unlocked(["sh", "-c", "exit 7"])
time.sleep(0.4)
c_pid = c.proc.pid
OUT["7_sibling_is_unreaped_zombie"] = state(c_pid) == "Z"
d = make_session("developer", 1002)
spawn(d)
cs._SESSIONS.clear()
cs._SESSIONS["architect"] = c
cs._SESSIONS["developer"] = d
d_kids = descendants_of(d.proc.pid)
d._kill_unlocked()  # runs the reaper with c tracked but unreaped
OUT["7_sibling_status_intact"] = c.proc.poll()
OUT["7_expected"] = 7
# 7b: the spin condition. A tracked, reapable, unpolled zombie (c) sits in
# front of the untracked orphans (d's descendants). A peek-based reaper
# returns c forever and never reaches them; the /proc scan does.
OUT["7b_had_tracked_zombie_in_front"] = state(c_pid) in ("Z", "ABSENT")
OUT["7b_orphans_created"] = len(d_kids)
OUT["7b_untracked_zombies_left"] = [
    p
    for p in (int(x) for x in os.listdir("/proc") if x.isdigit())
    if state(p) == "Z" and p not in cs._tracked_pids()
]

# --- (5) teardown reports failure honestly --------------------------------
cs._SESSIONS.clear()
e = make_session("architect", 1001)
spawn(e)
cs._SESSIONS["architect"] = e
OUT["5_shutdown_all_ok"] = cs.shutdown_all()
f = make_session("developer", 1002)
spawn(f)
cs._SESSIONS.clear()
cs._SESSIONS["developer"] = f
cs.LiveCliSession._kill_group_unlocked = lambda self, proc: "forced failure"
OUT["5_shutdown_all_failed"] = cs.shutdown_all()
cs.LiveCliSession._kill_group_unlocked = orig

print(json.dumps(OUT))
