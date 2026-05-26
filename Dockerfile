ARG K8S_HUB_VERSION=4.3.2

FROM python:3.12-bookworm AS wheel-build

# nodejs를 사용하여 에셋을 빌드합니다.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

RUN pip install --no-cache-dir build \
  && python -m build --wheel --outdir /dist

# hub의 이미지는 복잡한 제약 조건을 가집니다. 같은 동작을 보장하기 위해 이미 빌드된 이미지에 우리의 wheel-build를 덮어씌우는 전략을 취합니다.
FROM quay.io/jupyterhub/k8s-hub:${K8S_HUB_VERSION} AS base

USER root

COPY --from=wheel-build /dist/jupyterhub-*.whl /tmp/
COPY requirements-tmn.txt /tmp/

RUN pip install --no-cache-dir --no-deps --force-reinstall /tmp/jupyterhub-*.whl \
  && pip install --no-cache-dir -r /tmp/requirements-tmn.txt \
  && rm /tmp/jupyterhub-*.whl /tmp/requirements-tmn.txt


FROM base AS dev

COPY requirements-tmn-dev.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-tmn-dev.txt \
  && rm /tmp/requirements-tmn-dev.txt

USER 1000


FROM base AS prd

COPY requirements-tmn-prd.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-tmn-prd.txt \
  && rm /tmp/requirements-tmn-prd.txt

USER 1000
