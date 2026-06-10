"""DBSpawner: PostgreSQL 을 spawn lifecycle 의 SoT 로 쓰는 KubeSpawner.

multi-replica hub 를 위해 두 가지를 바꾼다.

1. PodReflector(informer watch) 를 쓰지 않는다. Pod 상태는 K8s API 단발 호출로 확인하고,
   "살아있는가"의 권위는 DB `spawners.phase` 가 가진다. 죽은 Pod 의 phase 보정은 hub-leader 의
   orphan reconcile 이 담당한다(별도 모듈).
2. lifecycle(start/stop/poll)을 DB phase 기반으로 재작성한다. Pod manifest 빌드·PVC 생성·
   삭제·URL 계산 같은 무거운 부분은 KubeSpawner 본체를 그대로 재사용한다.

`get_state`/`load_state`/`clear_state` 는 reflector 를 건드리지 않으므로 KubeSpawner 것을 그대로
쓴다. 식별은 Pod annotation `hub.jupyter.org/spawner-id`(= DB row id)로 한다. z2jh 표준 label 은
유지해 NetworkPolicy 등 부대 장비 호환을 깨지 않는다.
"""

from functools import partial

from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException
from kubespawner import KubeSpawner
from traitlets import default

from jupyterhub.orm import Phase
from jupyterhub.utils import exponential_backoff, maybe_future

# AIDEV-NOTE: DBSpawner 는 KubeSpawner 의 아래 메서드를 재사용한다. 전면 재작성(=manifest 빌드까지
# 복제)은 업스트림과 영구 분기를 만들기에, lifecycle 만 교체하고 빌더는 상속받는다. 업스트림이 이
# 심볼들을 바꾸면 import 시점에 즉시 실패시켜 조용한 동작 변화를 막는다.
for _m in (
    "get_pod_manifest",
    "get_pvc_manifest",
    "_make_create_pvc_request",
    "_make_delete_pod_request",
    "_get_pod_url",
    "is_pod_running",
    "_start",
    "load_user_options",
):
    if not hasattr(KubeSpawner, _m):
        raise ImportError(
            f"kubespawner API drift: KubeSpawner.{_m} not found. "
            "DBSpawner 를 현재 kubespawner 버전에 맞게 점검하라."
        )


class DBSpawner(KubeSpawner):
    """DB(PostgreSQL)가 spawn lifecycle SoT 인 KubeSpawner."""

    # Pod ↔ DB row 매핑용 annotation 키.
    SPAWNER_ID_ANNOTATION = "hub.jupyter.org/spawner-id"

    # --- reflector 진입점 차단 (watch 미사용) ---
    @property
    def pod_reflector(self):
        return None

    @property
    def event_reflector(self):
        return None

    async def _start_watching_pods(self, replace=False):
        return None

    async def _start_watching_events(self, replace=False):
        return None

    @default("events_enabled")
    def _events_enabled_default(self):
        # event reflector 를 쓰지 않으므로 progress() 에 공급할 k8s 이벤트 스트림이 없다.
        # 기본 True 로 두면 progress 가 빈 이벤트로 무의미하게 돈다.
        return False

    # --- 내부 헬퍼 ---
    def _set_phase(self, phase):
        # phase 만 갱신한다. started 등은 jupyterhub core 가 관리한다.
        self.orm_spawner.phase = phase
        self.db.commit()

    async def _read_pod(self):
        """자기 Pod 를 단발 GET. 없으면 None. reflector 와 동일한 camelCase dict 로 반환."""
        try:
            model = await self.api.read_namespaced_pod(self.pod_name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        # reflector 는 _preload_content=False 의 원본 dict 를 저장한다. 모델을 동일한
        # camelCase dict 로 변환해 is_pod_running / _get_pod_url 이 그대로 동작하게 한다.
        return self.api.api_client.sanitize_for_serialization(model)

    @staticmethod
    def _pod_failed(pod):
        return (pod.get("status") or {}).get("phase") == "Failed"

    def _tag_pod(self, pod):
        """Pod manifest 에 spawner-id annotation 을 추가한다(라벨은 KubeSpawner 것 유지)."""
        if pod.metadata.annotations is None:
            pod.metadata.annotations = {}
        pod.metadata.annotations[self.SPAWNER_ID_ANNOTATION] = str(self.orm_spawner.id)

    async def _create_pod_or_adopt(self, pod):
        """Pod 생성. 409 시 기존 Pod 를 죽이지 않는다(스톡 KubeSpawner 와 다른 점).

        같은 spawner-id 면 우리 spawn 의 Pod(재시도 또는 다른 replica 가 먼저 만든 것)이므로
        채택한다. 다른 spawner-id 면 남의 Pod 이므로 건드리지 않고 실패시킨다.
        """
        try:
            await self.api.create_namespaced_pod(self.namespace, pod)
            return
        except ApiException as e:
            if e.status != 409:
                raise
        existing = await self._read_pod()
        if existing is None:
            # 409 직후 사라짐. 다음 backoff 에서 재생성하도록 신호.
            raise RuntimeError(f"pod {self.pod_name} conflicted then vanished; retrying")
        ann = (existing.get("metadata") or {}).get("annotations") or {}
        if ann.get(self.SPAWNER_ID_ANNOTATION) == str(self.orm_spawner.id):
            self.log.info(
                "Adopting existing pod %s for spawner id=%s",
                self.pod_name,
                self.orm_spawner.id,
            )
            return
        raise RuntimeError(
            f"pod {self.pod_name} already exists for a different spawner; not deleting it"
        )

    async def _pod_is_ready(self):
        pod = await self._read_pod()
        if pod is None:
            return False
        if self._pod_failed(pod):
            raise RuntimeError(f"pod {self.pod_name} entered Failed phase")
        return self.is_pod_running(pod)

    # --- lifecycle ---
    # start() 는 KubeSpawner 의 sync wrapper 를 그대로 쓴다. wrapper 가 _start() 를
    # _start_future 로 감싸 progress() 의 종료 신호로 삼기 때문에, async start() 를 직접
    # override 하면 _start_future 가 비어 progress 가 끝나지 않는다.
    async def _start(self):
        self._set_phase(Phase.PENDING)
        try:
            # profile_list 의 kubespawner_override 와 user_options 를 spawner 속성에
            # 적용한다. helm 의 동적 설정(volumes/init_containers/uid/gid)이 전부 이 경로로
            # 들어오므로 get_pod_manifest() 전에 반드시 호출해야 한다.
            await self.load_user_options()

            if self.storage_pvc_ensure:
                pvc = self.get_pvc_manifest()
                await exponential_backoff(
                    partial(
                        self._make_create_pvc_request, pvc, self.k8s_api_request_timeout
                    ),
                    f"Could not create PVC {self.pvc_name}",
                    timeout=self.k8s_api_request_retry_timeout,
                )

            pod = await self.get_pod_manifest()
            if self.modify_pod_hook:
                pod = await maybe_future(self.modify_pod_hook(self, pod))
            self._tag_pod(pod)

            await self._create_pod_or_adopt(pod)
            self._set_phase(Phase.STARTING)

            await exponential_backoff(
                self._pod_is_ready,
                f"pod {self.pod_name} did not start in {self.start_timeout}s",
                timeout=self.start_timeout,
            )

            pod = await self._read_pod()
            self._set_phase(Phase.RUNNING)
            return self._get_pod_url(pod)
        except Exception:
            self._set_phase(Phase.FAILED)
            raise

    async def stop(self, now=False):
        self._set_phase(Phase.STOPPING)
        delete_options = client.V1DeleteOptions()
        grace_seconds = 0 if now else self.delete_grace_period
        delete_options.grace_period_seconds = grace_seconds

        await exponential_backoff(
            partial(
                self._make_delete_pod_request,
                self.pod_name,
                delete_options,
                grace_seconds,
                self.k8s_api_request_timeout,
            ),
            f"Could not delete pod {self.pod_name}",
            timeout=self.k8s_api_request_retry_timeout,
        )
        await exponential_backoff(
            lambda: self._pod_gone(),
            f"pod {self.pod_name} did not disappear in {self.start_timeout}s",
            timeout=self.start_timeout,
        )
        self._set_phase(Phase.STOPPED)

    async def _pod_gone(self):
        return (await self._read_pod()) is None

    async def poll(self):
        # DB 가 SoT. 다른 replica 의 phase 변경을 반영하기 위해 phase 를 refresh 한다.
        # phase 는 deferred 컬럼이라 attribute_names 로 명시해 한 번에 로드한다.
        # 죽은 Pod 의 phase 보정은 hub-leader 의 orphan reconcile 이 담당하므로 active phase
        # 의 hot path 에서는 K8s GET 을 하지 않는다.
        self.db.refresh(self.orm_spawner, ["phase"])
        phase = self.orm_spawner.phase
        if phase in (Phase.PENDING, Phase.STARTING, Phase.RUNNING):
            return None
        # 자가 치유: phase 가 비활성인데 실제 Pod 가 살아있으면 running 으로 승격한다.
        # KubeSpawner -> DBSpawner cutover 직후가 대표 사례다. 기존 row 는 migration default 로
        # phase='stopped' 인데 Pod 는 떠 있으므로, 이 GET 이 없으면 hub 가 init_spawners 에서
        # 살아있는 서버를 종료로 판정해 server row 와 라우트를 정리해 버린다(dev 에서 재현됨).
        pod = await self._read_pod()
        if pod is not None and self.is_pod_running(pod):
            self.log.info(
                "Pod %s is alive while phase=%s; adopting as running", self.pod_name, phase
            )
            self._set_phase(Phase.RUNNING)
            return None
        # stopped 는 정상 종료(0), failed 는 비정상(1).
        return 0 if phase == Phase.STOPPED else 1
