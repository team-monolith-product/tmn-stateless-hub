# syntax=docker/dockerfile:1.6
#
# tmn-stateless-hub 이미지
#
# wheel-build stage: 우리 fork 소스에서 jupyterhub wheel을 빌드한다. jupyterhub
# setup.py는 빌드 시점에 npm/sass/jsx를 호출하므로 node를 함께 설치한다.
#
# base stage: z2jh 표준 hub 이미지 위에 우리 wheel과 공통 의존성을
# (requirements-tmn.txt) 올린다. z2jh가 핀한 kubespawner·oauthenticator·
# idle-culler·sqlalchemy 의존성은 그대로 유지하고 jupyterhub Python 패키지만
# 우리 fork로 교체한다.
#
# tmn-stateless-hub-dev / tmn-stateless-hub-prd: base 위에 각 환경 전용
# 의존성(requirements-tmn-{env}.txt)만 더 install 한 분리 target. dev 에만
# 필요한 디버깅 도구 등을 prd 이미지에 흘리지 않기 위한 경계. workflow
# build-{env}.yml 의 dockerTarget 으로 1:1 매핑된다.

ARG K8S_HUB_VERSION=4.3.2

FROM python:3.12-bookworm AS wheel-build

# Node.js 20 (Debian bookworm 기본은 18.x, jupyterhub 빌드 스크립트는 20을 가정)
# git 은 setuptools_scm 의 file-finder 가 git ls-files 로 package_data 를
# 결정하기 위해 필요. 없으면 alembic.ini 등 비-`.py` 파일이 wheel 에
# 누락되어 런타임에 FileNotFoundError 발생.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist


FROM quay.io/jupyterhub/k8s-hub:${K8S_HUB_VERSION} AS base

USER root

COPY --from=wheel-build /dist/jupyterhub-*.whl /tmp/
COPY requirements-tmn.txt /tmp/

RUN pip install --no-cache-dir --no-deps --force-reinstall /tmp/jupyterhub-*.whl \
 && pip install --no-cache-dir -r /tmp/requirements-tmn.txt \
 && rm /tmp/jupyterhub-*.whl /tmp/requirements-tmn.txt


FROM base AS tmn-stateless-hub-dev

COPY requirements-tmn-dev.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-tmn-dev.txt \
 && rm /tmp/requirements-tmn-dev.txt

USER 1000


FROM base AS tmn-stateless-hub-prd

COPY requirements-tmn-prd.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-tmn-prd.txt \
 && rm /tmp/requirements-tmn-prd.txt

USER 1000
