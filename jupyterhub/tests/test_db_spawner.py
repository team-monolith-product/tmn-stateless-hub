"""Tests for jupyterhub.db_spawner.DBSpawner.

phase 상태 머신, 비파괴적 409 처리(adopt vs 거부), stop, poll(DB SoT), reflector 차단을
검증한다. K8s API 와 DB 는 mock 한다.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.rest import ApiException

from jupyterhub.db_spawner import DBSpawner
from jupyterhub.orm import Phase

ANN = DBSpawner.SPAWNER_ID_ANNOTATION


def _running_pod(spawner_id="1", ip="10.0.0.1"):
    return {
        "metadata": {"annotations": {ANN: spawner_id}},
        "status": {
            "phase": "Running",
            "podIP": ip,
            "containerStatuses": [{"name": "notebook", "ready": True}],
        },
    }


@pytest.fixture(autouse=True)
def offline_k8s_client(monkeypatch):
    """KubeSpawner.__init__ 의 load_config/shared_client 가 실제 클러스터를 읽지 않게 한다."""
    import kubespawner.spawner as ks

    monkeypatch.setattr(ks, "load_config", lambda *a, **k: None)
    monkeypatch.setattr(ks, "shared_client", lambda *a, **k: MagicMock())


def _make(monkeypatch, phase=Phase.STOPPED):
    s = DBSpawner(_mock=True)
    # server=None 이어야 Spawner._orm_spawner_changed observer 가 .server 분기를 건너뛴다.
    s.orm_spawner = SimpleNamespace(id=1, phase=phase, started=None, server=None)
    s.db = MagicMock()
    s.api = MagicMock()
    s.pod_name = "jupyter-test"
    s.namespace = "default"
    s.storage_pvc_ensure = False

    phases = []
    orig = s._set_phase

    def rec(p):
        phases.append(p)
        orig(p)

    monkeypatch.setattr(s, "_set_phase", rec)
    return s, phases


def _manifest():
    return MagicMock(metadata=MagicMock(annotations=None))


async def test_start_happy_path(monkeypatch):
    s, phases = _make(monkeypatch)
    monkeypatch.setattr(s, "get_pod_manifest", AsyncMock(return_value=_manifest()))
    s.api.create_namespaced_pod = AsyncMock()
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=_running_pod()))
    monkeypatch.setattr(s, "_get_pod_url", lambda pod: "http://10.0.0.1:8888")

    url = await s.start()

    assert url == "http://10.0.0.1:8888"
    assert phases == [Phase.PENDING, Phase.STARTING, Phase.RUNNING]
    s.api.create_namespaced_pod.assert_awaited_once()


async def test_start_409_adopts_own_pod(monkeypatch):
    """같은 spawner-id 의 기존 Pod 는 죽이지 않고 채택한다(다른 replica 가 먼저 만든 경우)."""
    s, phases = _make(monkeypatch)
    monkeypatch.setattr(s, "get_pod_manifest", AsyncMock(return_value=_manifest()))
    s.api.create_namespaced_pod = AsyncMock(
        side_effect=ApiException(status=409, reason="Conflict")
    )
    s.api.delete_namespaced_pod = AsyncMock()
    monkeypatch.setattr(s, "_make_delete_pod_request", AsyncMock())
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=_running_pod("1")))
    monkeypatch.setattr(s, "_get_pod_url", lambda pod: "http://10.0.0.1:8888")

    url = await s.start()

    assert url == "http://10.0.0.1:8888"
    assert phases[-1] == Phase.RUNNING
    s.api.delete_namespaced_pod.assert_not_awaited()
    s._make_delete_pod_request.assert_not_awaited()


async def test_start_409_foreign_pod_fails_without_delete(monkeypatch):
    """다른 spawner-id 의 Pod 는 건드리지 않고 실패시킨다."""
    s, phases = _make(monkeypatch)
    monkeypatch.setattr(s, "get_pod_manifest", AsyncMock(return_value=_manifest()))
    s.api.create_namespaced_pod = AsyncMock(
        side_effect=ApiException(status=409, reason="Conflict")
    )
    s.api.delete_namespaced_pod = AsyncMock()
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=_running_pod("999")))

    with pytest.raises(RuntimeError):
        await s.start()

    assert phases[-1] == Phase.FAILED
    s.api.delete_namespaced_pod.assert_not_awaited()


async def test_stop_transitions_and_deletes(monkeypatch):
    s, phases = _make(monkeypatch, phase=Phase.RUNNING)
    monkeypatch.setattr(s, "_make_delete_pod_request", AsyncMock(return_value=True))
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=None))

    await s.stop()

    assert phases == [Phase.STOPPING, Phase.STOPPED]
    s._make_delete_pod_request.assert_awaited()


@pytest.mark.parametrize(
    "phase,expected",
    [
        (Phase.RUNNING, None),
        (Phase.STARTING, None),
        (Phase.PENDING, None),
        (Phase.STOPPED, 0),
        (Phase.FAILED, 1),
    ],
)
async def test_poll_reads_db_phase(monkeypatch, phase, expected):
    s, _ = _make(monkeypatch, phase=phase)
    s.db.refresh = MagicMock()

    assert await s.poll() == expected
    s.db.refresh.assert_called_once_with(s.orm_spawner)


async def test_reflector_disabled(monkeypatch):
    s, _ = _make(monkeypatch)
    assert s.pod_reflector is None
    assert s.event_reflector is None
    assert await s._start_watching_pods() is None
    assert await s._start_watching_events() is None
