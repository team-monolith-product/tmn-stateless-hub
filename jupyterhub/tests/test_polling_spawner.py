"""Tests for jupyterhub.polling_spawner.PollingKubeSpawner.

핵심 검증은 watch -> 주기적 list 치환이다(reflector 주입, list 반복, 실패 내성). spawn
lifecycle 은 KubeSpawner 본체를 그대로 재사용하므로, poll 관련 테스트는 재사용한 poll() 이
polling reflector 캐시(camelCase dict)를 정상적으로 읽는지 확인하는 통합 가드로 둔다.
"""

import asyncio
import logging

import pytest

from jupyterhub.polling_spawner import (
    PollingEventReflector,
    PollingKubeSpawner,
    PollingPodReflector,
    _PollingReflectorMixin,
)

log = logging.getLogger("test.polling_spawner")


class _StubReflector(_PollingReflectorMixin):
    """ResourceReflector 의 무거운 __init__(api client 등) 없이 production 의
    _watch_and_update 만 정상 경로로 테스트하기 위한 stub. _watch_and_update 는
    mixin 에서 그대로 상속하므로 PollingPodReflector/PollingEventReflector 와 동일하다."""

    def __init__(self, list_fn, interval=0.0):
        self.poll_interval_seconds = interval
        self.log = log
        self._list_and_update = list_fn


def _running_pod(name, ip="10.0.0.1"):
    """is_pod_running 이 Running 으로 판정하는 최소 Pod dict(camelCase)."""
    return {
        "metadata": {"name": name, "uid": "uid-1"},
        "status": {
            "phase": "Running",
            "podIP": ip,
            "containerStatuses": [
                {"name": "notebook", "ready": True, "state": {"running": {}}}
            ],
        },
    }


async def test_start_watching_injects_polling_reflectors(monkeypatch):
    """_start_watching_pods/_events 가 polling reflector 클래스를 주입한다."""
    spawner = PollingKubeSpawner(_mock=True)
    spawner.poll_interval_seconds = 3.0

    captured = {}

    async def fake_start_reflector(kind, reflector_class, **kwargs):
        captured[kind] = (reflector_class, kwargs)
        return object()

    monkeypatch.setattr(spawner, "_start_reflector", fake_start_reflector)

    await spawner._start_watching_pods()
    await spawner._start_watching_events()

    assert captured["pods"][0] is PollingPodReflector
    assert captured["events"][0] is PollingEventReflector
    # spawner config 가 reflector 로 전달된다.
    assert captured["pods"][1]["poll_interval_seconds"] == 3.0
    assert captured["events"][1]["poll_interval_seconds"] == 3.0
    # 기존 selector 규약(표준 label/field)을 그대로 유지한다.
    assert captured["pods"][1]["labels"] == {"component": spawner.component_label}
    assert captured["events"][1]["fields"] == {"involvedObject.kind": "Pod"}


async def test_polling_reflector_lists_periodically():
    """polling reflector 는 watch 가 아니라 _list_and_update 를 주기 호출한다."""
    calls = 0

    async def fake_list_and_update():
        nonlocal calls
        calls += 1
        if calls >= 3:
            # 루프 종료를 위해 reflector.stop() 의 cancel 을 흉내낸다.
            raise asyncio.CancelledError()

    reflector = _StubReflector(fake_list_and_update)

    with pytest.raises(asyncio.CancelledError):
        await reflector._watch_and_update()

    assert calls == 3


async def test_polling_reflector_survives_list_error():
    """일시적 list 실패가 폴링 루프를 죽이지 않고 다음 주기에 재시도한다."""
    calls = 0

    async def flaky_list():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient API error")
        if calls >= 3:
            raise asyncio.CancelledError()

    reflector = _StubReflector(flaky_list)

    with pytest.raises(asyncio.CancelledError):
        await reflector._watch_and_update()

    assert calls == 3


async def test_poll_running_pod_returns_none(monkeypatch):
    """super().poll() 가 polling reflector 캐시를 읽어 running 이면 None 을 돌려준다."""
    spawner = PollingKubeSpawner(_mock=True)
    spawner.namespace = "default"
    spawner.pod_name = "jupyter-test"
    ref_key = f"{spawner.namespace}/{spawner.pod_name}"

    async def noop(replace=False):
        return None

    monkeypatch.setattr(spawner, "_start_watching_pods", noop)

    class _FakeReflector:
        pods = {ref_key: _running_pod(spawner.pod_name)}

    key = spawner._get_reflector_key("pods")
    monkeypatch.setitem(PollingKubeSpawner.reflectors, key, _FakeReflector())

    assert await spawner.poll() is None


async def test_poll_missing_pod_returns_exit_code(monkeypatch):
    """캐시에 pod 가 없으면 poll() 은 1(종료)을 돌려준다."""
    spawner = PollingKubeSpawner(_mock=True)
    spawner.namespace = "default"
    spawner.pod_name = "jupyter-test"

    async def noop(replace=False):
        return None

    monkeypatch.setattr(spawner, "_start_watching_pods", noop)

    class _EmptyReflector:
        pods = {}

    key = spawner._get_reflector_key("pods")
    monkeypatch.setitem(PollingKubeSpawner.reflectors, key, _EmptyReflector())

    assert await spawner.poll() == 1
