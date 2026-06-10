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
    # profile_list 평가는 upstream 책임이므로 호출 여부만 검증한다.
    monkeypatch.setattr(s, "load_user_options", AsyncMock())

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
    # helm 의 동적 설정(profile_list kubespawner_override)이 적용되는 유일한 경로.
    s.load_user_options.assert_awaited_once()
    # 부모 start() wrapper 가 _start_future 를 세팅해야 progress() 가 종료를 감지한다.
    assert s._start_future is not None and s._start_future.done()


async def test_start_wrapper_not_overridden():
    """start() 는 KubeSpawner 의 sync wrapper(_start_future 세팅)를 그대로 써야 한다."""
    from kubespawner import KubeSpawner

    assert DBSpawner.start is KubeSpawner.start
    assert DBSpawner._start is not KubeSpawner._start


async def test_events_disabled_by_default(monkeypatch):
    """event reflector 가 없으므로 progress 의 이벤트 스트림도 기본 비활성."""
    s, _ = _make(monkeypatch)
    assert s.events_enabled is False


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
    # 비활성 phase 의 자가 치유 GET 은 Pod 부재로 응답.
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=None))

    assert await s.poll() == expected
    s.db.refresh.assert_called_once_with(s.orm_spawner, ["phase"])
    if expected is None:
        # active phase 의 hot path 는 K8s GET 을 하지 않는다.
        s._read_pod.assert_not_awaited()


async def test_poll_adopts_live_pod_when_phase_inactive(monkeypatch):
    """cutover 자가 치유: phase=stopped 인데 Pod 가 살아있으면 running 으로 승격한다."""
    s, phases = _make(monkeypatch, phase=Phase.STOPPED)
    s.db.refresh = MagicMock()
    monkeypatch.setattr(s, "_read_pod", AsyncMock(return_value=_running_pod()))

    assert await s.poll() is None
    assert phases == [Phase.RUNNING]


async def test_reflector_disabled(monkeypatch):
    s, _ = _make(monkeypatch)
    assert s.pod_reflector is None
    assert s.event_reflector is None
    assert await s._start_watching_pods() is None
    assert await s._start_watching_events() is None
