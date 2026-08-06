"""Resource Governor hysteresis — trip < floor, reset > resume."""

from __future__ import annotations

from pathlib import Path

from agentd.db import Store
from agentd.governor import ResourceGovernor


def test_trip_and_reset_hysteresis(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    notices: list[tuple[str, str]] = []
    free = {"gb": 100.0}

    gov = ResourceGovernor(
        store,
        tmp_path,
        floor_gb=15.0,
        resume_gb=20.0,
        interval_s=60,
        notify=lambda msg, title: notices.append((msg, title)),
        free_gb_fn=lambda _p: free["gb"],
    )

    assert gov.sample_once() == 100.0
    assert store.is_disk_paused() is False
    assert notices == []

    free["gb"] = 12.0
    assert gov.sample_once() == 12.0
    assert store.is_disk_paused() is True
    assert "12.0" in (store.breaker_state()["reason"] or "")
    assert len(notices) == 1

    # Between floor and resume: stay tripped (no flap).
    free["gb"] = 17.0
    gov.sample_once()
    assert store.is_disk_paused() is True
    assert len(notices) == 1

    free["gb"] = 22.0
    gov.sample_once()
    assert store.is_disk_paused() is False
    store.close()


def test_no_retrip_while_already_paused(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    notices: list[str] = []
    gov = ResourceGovernor(
        store,
        tmp_path,
        floor_gb=15.0,
        resume_gb=20.0,
        notify=lambda msg, title: notices.append(msg),
        free_gb_fn=lambda _p: 10.0,
    )
    gov.sample_once()
    gov.sample_once()
    assert len(notices) == 1
    store.close()
