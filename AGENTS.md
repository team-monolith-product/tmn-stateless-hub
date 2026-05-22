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

본 레포는 Python 패키지 fork만 보유한다. 컨테이너 이미지 빌드 워크플로우는 후속 PR에서 추가된다(INF-227 stack 1B). 빌드된 이미지는 ECR `tmn-ecr-stateless-hub-all-{dev,prd}`로 푸시되고 `enk-opencode-hub-helm` chart의 `jupyterhub.hub.image`로 참조된다.

## 로컬 검증

- 테스트: `pytest jupyterhub/tests` (upstream과 동일).
- 의존성: `pip install -e .` 또는 `pip install -r requirements.txt`.
- lint: upstream `pyproject.toml`의 ruff/flake8 설정 그대로 사용.
