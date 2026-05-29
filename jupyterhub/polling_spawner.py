"""watch 대신 주기적 list 로 pod/event 상태를 갱신하는 KubeSpawner.

multi-replica hub 에서 KubeSpawner 의 ``PodReflector`` 는 replica 마다 apiserver 에 watch
연결을 열어두고, 그 watch 가 채우는 in-memory 캐시를 replica 마다 따로 들고 있다. 이 모듈은
reflector 의 watch 루프만 주기적 list 로 바꿔, watch 연결을 없애고 캐시를 주기적 list 로 채운다.
spawn lifecycle(start/stop/poll/load_state 등)은 KubeSpawner 본체를 그대로 재사용한다.

KubeSpawner 가 reflector 를 읽는 지점은 모두 ``self.pod_reflector.pods`` 의 동기 dict 접근이라
캐시를 누군가 갱신해 주어야 한다. ResourceReflector 는 ``_list_and_update`` 로 캐시를 채우고
``_watch_and_update`` 로 watch 루프를 도는 구조이므로, ``_watch_and_update`` 만 주기적
``_list_and_update`` 로 치환하면 캐시 구조와 키 형식, ``first_load_future``, ``start``/``stop`` 을
그대로 쓰면서 watch 만 사라진다.
"""

import asyncio

from kubespawner.spawner import EventReflector, KubeSpawner, PodReflector
from traitlets import Float

# AIDEV-NOTE: 이 모듈은 kubespawner 7.0.0 의 internal 심볼에 의존한다(z2jh 4.3.2 이미지 기준).
# 대안인 lifecycle 메서드 전면 재작성은 PVC/secret/service 생성 흐름 수백 줄을 복제해 업스트림과
# 영구 분기를 만들기에, watch 루프 한 곳만 치환하는 쪽을 택했다. 업스트림이 아래 심볼을 바꾸면
# import 시점에 즉시 실패시켜 조용한 동작 변화를 막는다.
for _attr, _owner in (
    ("_start_reflector", KubeSpawner),
    ("_watch_and_update", PodReflector),
    ("_list_and_update", PodReflector),
    ("_watch_and_update", EventReflector),
    ("_list_and_update", EventReflector),
):
    if not hasattr(_owner, _attr):
        raise ImportError(
            f"kubespawner API drift: {_owner.__name__}.{_attr} not found. "
            "PollingKubeSpawner 를 현재 kubespawner 버전에 맞게 점검하라."
        )


class _PollingReflectorMixin:
    """ResourceReflector 의 watch 루프를 주기적 list 로 대체하는 mixin.

    AIDEV-NOTE: poll_interval_seconds 트레이트는 concrete 서브클래스에 선언한다. traitlets
    메타클래스는 HasTraits 하위 클래스의 __dict__ 에 있는 트레이트만 name 을 등록하므로, 이
    plain mixin 에 두면 name 미등록으로 set 시 AssertionError 가 난다.
    """

    async def _watch_and_update(self):
        # ResourceReflector.start() 가 첫 _list_and_update() 를 이미 수행한 뒤 이 코루틴을 task 로
        # 띄운다. 따라서 여기서는 sleep 후 list 를 반복한다. 취소는 reflector.stop() 이 watch_task 를
        # cancel 하여 CancelledError 로 전파되며, 아래 except 는 이를 잡지 않는다.
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            try:
                await self._list_and_update()
            except Exception:
                # 일시적 list 실패로 폴링이 죽지 않도록 다음 주기에 재시도한다.
                self.log.exception(
                    "Polling reflector list failed; retrying next interval"
                )


class PollingPodReflector(_PollingReflectorMixin, PodReflector):
    # watch 가 없으므로 이 주기로 list 하여 캐시를 갱신한다. spawn 상태 감지 지연의 상한이 된다.
    poll_interval_seconds = Float(5.0)


class PollingEventReflector(_PollingReflectorMixin, EventReflector):
    poll_interval_seconds = Float(5.0)


class PollingKubeSpawner(KubeSpawner):
    """PodReflector watch 대신 주기적 list 를 쓰는 KubeSpawner.

    ``c.JupyterHub.spawner_class`` 로 지정해 사용한다. 기존 ``c.KubeSpawner.*`` 설정은
    KubeSpawner 의 subclass 이므로 traitlets MRO 로 그대로 적용된다.
    """

    poll_interval_seconds = Float(
        5.0,
        config=True,
        help=(
            "reflector 가 watch 대신 pod/event 를 list 하는 주기(초). "
            "spawn 상태 감지 지연의 상한이며, list 실패 시에도 이 주기로 재시도한다"
            "(별도 backoff 없음). 너무 낮추면 apiserver 부하가 replica 수만큼 증폭되니 "
            "주의하라. 설정은 reflector 단위 트레이트가 아니라 이 값으로만 한다."
        ),
    )

    async def _start_watching_pods(self, replace=False):
        # KubeSpawner._start_watching_pods 와 동일하되 reflector_class 만 polling 으로 교체한다.
        return await self._start_reflector(
            kind="pods",
            reflector_class=PollingPodReflector,
            labels={"component": self.component_label},
            omit_namespace=self.enable_user_namespaces,
            replace=replace,
            poll_interval_seconds=self.poll_interval_seconds,
        )

    async def _start_watching_events(self, replace=False):
        return await self._start_reflector(
            kind="events",
            reflector_class=PollingEventReflector,
            fields={"involvedObject.kind": "Pod"},
            omit_namespace=self.enable_user_namespaces,
            replace=replace,
            poll_interval_seconds=self.poll_interval_seconds,
        )
