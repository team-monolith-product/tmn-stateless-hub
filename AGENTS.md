# AGENTS.md

## 정체성

본 레포는 [`jupyterhub/jupyterhub`](https://github.com/jupyterhub/jupyterhub)의 fork이며 `main` 브랜치는 upstream 태그 `5.4.6`을 시작점으로 한다. upstream `main`(현재 `6.0.0.dev`)의 메이저 변경은 따라가지 않고 `5.4.x` 라인 안에서만 우리 패치를 쌓는다. z2jh chart 4.3.2가 jupyterhub 5.4.3을 핀해 사용하므로 5.4.x 계열을 유지해야 호환된다.

## 변경 정책

- 우리 변경은 모두 INF 계열 task와 1:1 매핑된 feature 브랜치 → `main` PR로 통합된다.
- upstream의 patch 릴리즈(5.4.7, 5.4.8, ...)는 별도 cherry-pick branch로 `git fetch upstream tag 5.4.X` 후 `git cherry-pick`으로 가져온다. 메이저/마이너 점프(5.5, 6.x)는 별도 평가 후 결정.
- 우리 변경은 가능한 한 thin patch로 유지하고, 큰 구조 변경은 별도 모듈로 분리해 upstream 파일 수정량을 최소화한다.

## upstream sync 절차

```bash
git remote add upstream https://github.com/jupyterhub/jupyterhub.git  # 1회만
git fetch upstream tag 5.4.X
git checkout -b sync/upstream-5.4.X main
git cherry-pick <commits>  # 필요한 커밋만 선별
gh pr create --base main --title "sync: upstream 5.4.X" --draft
```

## PR 절차

- 브랜치 이름: `feature/<TASK-ID>-<설명>` 또는 `fix/<TASK-ID>-<설명>`.
- PR 대상: `main`. assignee: `BrianPark314`. draft로 시작.
- 제목 형식: `<TASK-ID> <설명>`.
- 다단계 작업은 stacked PR 패턴(PR2 base를 PR1 브랜치로) 사용.

## 빌드 및 배포

본 레포는 jupyterhub fork 의 Python wheel 만 생산한다. 컨테이너 이미지 빌드는 [`tmn-dockerfile`](https://github.com/team-monolith-product/tmn-dockerfile) 의 `stateless-hub/` 디렉토리에서 별도로 수행된다 ([`jce-codle-jlext`](https://github.com/team-monolith-product/jce-codle-jlext) 가 `jce-js-dockerfile` 로 분리한 패턴과 동일).

흐름:
- `develop` push → `upload-package-dev.yml` → `tbump` 로 `pyproject.toml`/`jupyterhub/_version.py`/`docs/source/_static/rest-api.yml` 의 version 을 `X.Y.ZbN` 형식으로 자동 +1 → develop 에 commit/push → `python -m build --wheel` → nexus `https://nexus.dev.codle.io/repository/pypi-internal/` 에 publish → `tmn-dockerfile/stateless-hub/requirements-dev-server.txt` 의 jupyterhub pin 자동 갱신 commit → tmn-dockerfile 의 build CI 트리거.
- `main` push → `upload-package-prd.yml` → **bump 없이** publish → tmn-dockerfile prd requirements pin 갱신. 같은 version 으로 재 publish 시 nexus 가 reject 해 workflow fail; main 의 `pyproject.toml` version 을 PR 머지 전 사람이 수동 변경해 둔다.

upstream 의 `[tool.tbump]` 설정을 그대로 활용 (PEP 440 `(a|b|rc)\d+` regex 지원). 매 dev push 마다 develop 브랜치에 `auto: 버전 업 → X.Y.ZbN` 자동 commit 이 추가되며, tbump 가 갱신하는 세 파일이 함께 변경된다. upstream sync 시 이 세 파일에서 우리 fork 의 version 값이 충돌 가능 — 우리 값 채택으로 resolve.

## 로컬 검증

- 테스트: `pytest jupyterhub/tests` (upstream과 동일).
- 의존성: `pip install -e .` 또는 `pip install -r requirements.txt`.
- lint: upstream `pyproject.toml`의 ruff/flake8 설정 그대로 사용.
- wheel 빌드: `pip install build && python -m build --wheel --outdir dist/`. `unzip -l dist/jupyterhub-*.whl | grep alembic.ini` 로 비-py 파일 포함 확인.
- version bump 미리보기: `pip install tbump && tbump --non-interactive --only-patch --dry-run 5.4.6bN` 로 어떤 파일들이 어떻게 변경되는지 확인.
