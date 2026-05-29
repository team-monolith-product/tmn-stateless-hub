"""Tests for jupyterhub.hub_leader.HubLeader (k8s Lease 선출 + callback registry)."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kubernetes_asyncio.client.rest import ApiException

from jupyterhub.hub_leader import HubLeader

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _leader(api, identity="me"):
    return HubLeader(
        api,
        "ns",
        identity,
        lease_duration_s=30,
        renew_interval_s=10,
        now=lambda: NOW,
    )


def _lease(holder, renew_time, transitions=0):
    return SimpleNamespace(
        spec=SimpleNamespace(
            holder_identity=holder,
            renew_time=renew_time,
            lease_duration_seconds=30,
            lease_transitions=transitions,
            acquire_time=renew_time,
        )
    )


async def test_acquire_creates_lease_when_absent():
    api = MagicMock()
    api.read_namespaced_lease = AsyncMock(side_effect=ApiException(status=404))
    api.create_namespaced_lease = AsyncMock()
    api.replace_namespaced_lease = AsyncMock()

    assert await _leader(api)._acquire_or_renew() is True
    api.create_namespaced_lease.assert_awaited_once()


async def test_renew_when_we_hold():
    api = MagicMock()
    api.read_namespaced_lease = AsyncMock(return_value=_lease("me", NOW))
    api.replace_namespaced_lease = AsyncMock()

    assert await _leader(api)._acquire_or_renew() is True
    api.replace_namespaced_lease.assert_awaited_once()


async def test_not_leader_when_other_holds_fresh_lease():
    api = MagicMock()
    api.read_namespaced_lease = AsyncMock(return_value=_lease("other", NOW))
    api.replace_namespaced_lease = AsyncMock()

    assert await _leader(api)._acquire_or_renew() is False
    api.replace_namespaced_lease.assert_not_awaited()


async def test_takeover_when_lease_expired():
    api = MagicMock()
    expired = _lease("other", NOW - timedelta(seconds=60))
    api.read_namespaced_lease = AsyncMock(return_value=expired)
    api.replace_namespaced_lease = AsyncMock()

    assert await _leader(api)._acquire_or_renew() is True
    api.replace_namespaced_lease.assert_awaited_once()
    # takeover 시 holder 가 우리로 바뀌고 transitions 증가
    sent = api.replace_namespaced_lease.await_args.args[2]
    assert sent.spec.holder_identity == "me"
    assert sent.spec.lease_transitions == 1


async def test_renew_conflict_returns_false():
    api = MagicMock()
    api.read_namespaced_lease = AsyncMock(return_value=_lease("me", NOW))
    api.replace_namespaced_lease = AsyncMock(side_effect=ApiException(status=409))

    assert await _leader(api)._acquire_or_renew() is False


async def test_registry_starts_and_stops_callbacks():
    api = MagicMock()
    leader = _leader(api)
    ran = asyncio.Event()

    async def cb():
        ran.set()

    leader.register(cb, interval_s=0.01)
    leader._start_tasks()
    await asyncio.wait_for(ran.wait(), timeout=1)
    assert len(leader._handles) == 1

    leader._stop_tasks()
    assert leader._handles == []
