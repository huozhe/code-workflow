"""#233 + #172: `--cap-drop ALL` denies uid 0 twice, and both need the role uid.

`CAP_DAC_OVERRIDE` is what lets root read the role's `0700` token dir; `CAP_KILL`
is what lets it signal the role's CLI. §7.2 drops both, so the runner must borrow
the role's identity for each. The kill learned that (ADR-35 / #221); the wipe did
not (#233), and this file is the wipe's half.

The kill's half is **already** `test_adr35_kill_in_image.py::test_2_no_descendant_survives_the_kill`,
which drives `_kill_unlocked` with a child held open — item 6's actual ask. Do not
add a second, helper-level answer to that question here: a test calling
`_killpg_as_role` stays green when `_kill_unlocked` stops calling it.

**These fixtures must run inside the image with the production capability set.**
On an unprivileged host where the runner and the role share a uid, the `EPERM`
never happens and the assertions pass against the broken code — which is the trap
#233's acceptance names. So each test asserts the capability set *first*, and a
host that cannot reach the case fails rather than passes.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator

import pytest

# Linux capability bit numbers (`man 7 capabilities`).
_CAP_DAC_OVERRIDE = 1

_UID_ARCHITECT = 1001


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


@pytest.fixture
def prod_caps_container(session_runner_image: str) -> Iterator[str]:
    """A throwaway container with **supervisor.py's** flags, not defaults.

    Every flag here is load-bearing for what these tests assert: drop the
    capability set and the `EPERM`s disappear, and both tests pass against code
    that cannot work in production.
    """
    name = f"agentd-caps-{uuid.uuid4().hex[:8]}"
    r = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "--cap-drop", "ALL",
            "--cap-add", "CHOWN",
            "--cap-add", "FOWNER",
            "--cap-add", "SETUID",
            "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/run/agent:rw,noexec,nosuid,size=1m,mode=0711",
            "--entrypoint", "sleep",
            session_runner_image, "600",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        pytest.fail(f"container start failed: {r.stderr or r.stdout}")
    try:
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _exec(container: str, script: str, *, user: str | None = None) -> str:
    args = ["docker", "exec"]
    if user is not None:
        args += ["-u", user]
    args += [container, "python3", "-c", script]
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.fail(f"exec failed rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    return r.stdout.strip()


def _cap_eff(container: str) -> int:
    out = _exec(
        container,
        "print([l.split()[1] for l in open('/proc/1/status') "
        "if l.startswith('CapEff:')][0])",
    )
    return int(out, 16)


def test_the_container_actually_denies_uid_0(prod_caps_container: str) -> None:
    """Guard for every test below. If this fails, they prove nothing."""
    caps = _cap_eff(prod_caps_container)
    assert not caps & (1 << _CAP_DAC_OVERRIDE), f"CapEff {caps:#018x} has DAC_OVERRIDE"


def test_root_cannot_read_the_role_token_dir(prod_caps_container: str) -> None:
    """#233's premise, measured rather than quoted: `is_dir()` True, `iterdir()` EPERM."""
    caps = _cap_eff(prod_caps_container)
    assert not caps & (1 << _CAP_DAC_OVERRIDE)

    out = _exec(
        prod_caps_container,
        f"""
import json, os
os.makedirs('/run/agent/architect', exist_ok=True)
open('/run/agent/architect/token', 'w').write('secret')
os.chown('/run/agent/architect/token', {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chown('/run/agent/architect', {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chmod('/run/agent/architect', 0o700)
res = {{'is_dir': os.path.isdir('/run/agent/architect')}}
try:
    os.listdir('/run/agent/architect')
    res['listdir_errno'] = 0
except OSError as e:
    res['listdir_errno'] = e.errno
print(json.dumps(res))
""",
    )
    got = json.loads(out)
    assert got["is_dir"] is True
    assert got["listdir_errno"] != 0, (
        "root could read the role dir — the capability set is not production's, "
        "so #233's fix would look correct here whether or not it works"
    )


def test_teardown_wipes_the_role_dir_and_reports_the_kill(
    prod_caps_container: str,
) -> None:
    """#233 acceptance, driven through `session.teardown` — not the helper.

    Calling `_wipe_role_tokens_as_role` directly would prove the helper works and
    stay green with the call deleted from teardown, which is the same fixture
    defect this repository keeps finding. `handle_request` is the site.

    Asserts all three halves: the RPC succeeds instead of `-32000`, the role's
    tmpfs dir is empty afterwards, and the result is the *kill's*, not the wipe's.
    """
    caps = _cap_eff(prod_caps_container)
    assert not caps & (1 << _CAP_DAC_OVERRIDE)

    out = _exec(
        prod_caps_container,
        f"""
import json, os
from pathlib import Path
from agentd_runner import server
d = Path('/run/agent/architect')
os.makedirs(d, exist_ok=True)
for n in ('token', 'model-creds'):
    open(d / n, 'w').write('secret')
    os.chown(d / n, {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chown(d, {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chmod(d, 0o700)

# Root cannot even count them — that is the premise, asserted in the fixture.
try:
    os.listdir(d); reachable = False
except OSError:
    reachable = True

server.STATE.initialized = True
resp = server.handle_request(
    {{'jsonrpc': '2.0', 'id': 1, 'method': 'session.teardown', 'params': {{}}}},
    True,
)
pid = os.fork()
if pid == 0:
    os.setgid({_UID_ARCHITECT}); os.setuid({_UID_ARCHITECT})
    os._exit(len(os.listdir(d)))
_, st = os.waitpid(pid, 0)
print(json.dumps({{
    'reachable': reachable,
    'resp': resp,
    'left': os.WEXITSTATUS(st),
}}))
""",
    )
    got = json.loads(out)
    assert got["reachable"] is True, (
        "root could read the role dir — this host is not production's capability "
        "set, so the fix would look correct here whether or not it works"
    )
    assert "error" not in got["resp"], (
        f"teardown returned an error: {got['resp'].get('error')} — before #233 this "
        "was -32000 from the wipe crashing, reported as a failed kill"
    )
    assert got["resp"]["result"] == {"torn_down": True}
    assert got["left"] == 0, f"{got['left']} secret(s) still in the role's tmpfs dir"


def test_a_wipe_failure_does_not_mask_a_kill_failure(
    prod_caps_container: str,
) -> None:
    """#233 acceptance 3. The two must stay distinguishable.

    Before the fix the wipe crashed first, so a *genuine* surviving CLI surfaced
    as the same opaque `-32000` as a permission error on a directory. `-32003`
    exists precisely to say the kill failed, and was unreachable.
    """
    out = _exec(
        prod_caps_container,
        f"""
import json, os
from pathlib import Path
from agentd_runner import cli_session, server
d = Path('/run/agent/architect')
os.makedirs(d, exist_ok=True)
open(d / 'token', 'w').write('secret')
os.chown(d / 'token', {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chown(d, {_UID_ARCHITECT}, {_UID_ARCHITECT})
os.chmod(d, 0o000)                      # wipe cannot succeed, even as the role
cli_session.shutdown_all = lambda: ['architect: cli still running']
server.STATE.initialized = True
resp = server.handle_request(
    {{'jsonrpc': '2.0', 'id': 1, 'method': 'session.teardown', 'params': {{}}}},
    True,
)
print(json.dumps(resp))
""",
    )
    resp = json.loads(out)
    assert "error" in resp, "a surviving CLI must not report success"
    assert resp["error"]["code"] == -32003, (
        f"got {resp['error']} — the wipe failure masked the kill failure, which is "
        "the third consequence #233 names"
    )
    assert "cli still running" in resp["error"]["message"]
