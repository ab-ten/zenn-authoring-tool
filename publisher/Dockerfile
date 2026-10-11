ARG PYTHON_VERSION=3.13
ARG DEBIAN_VARIANT=slim-trixie
ARG GIT_VERSION=2.51.1

FROM python:${PYTHON_VERSION}-${DEBIAN_VARIANT} AS git-builder

ARG GIT_VERSION
ARG GIT_URL=https://mirrors.edge.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz
ARG BUILD_PACKAGES="build-essential gettext libcurl4-openssl-dev libexpat1-dev libssl-dev zlib1g-dev tcl pkg-config"

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils make perl ${BUILD_PACKAGES}; \
    mkdir -p /usr/src/git /git-install; \
    curl -fsSL "${GIT_URL}" -o /tmp/git.tar.xz; \
    tar -xJf /tmp/git.tar.xz -C /usr/src/git --strip-components=1; \
    cd /usr/src/git; \
    make prefix=/usr/local all; \
    make prefix=/usr/local DESTDIR=/git-install install; \
    cd /; \
    rm -rf /usr/src/git /tmp/git.tar.xz /var/lib/apt/lists/*


FROM python:${PYTHON_VERSION}-${DEBIAN_VARIANT} AS runtime

ARG RUNTIME_PACKAGES="ca-certificates"

# Git の実行時依存パッケージをインストール
COPY --from=git-builder /git-install/ /

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ${RUNTIME_PACKAGES}; \
    ln -fs /usr/share/zoneinfo/Asia/Tokyo /etc/localtime;\
    dpkg-reconfigure -f noninteractive tzdata;\
    update-ca-certificates --fresh;\
    rm -rf /var/lib/apt/lists/*; \
    git --version; \
    ldd /usr/local/libexec/git-core/git-remote-https > /dev/null
# ldd は git clone などが libexpat を必要としているのではないかという疑惑があったためチェックしている

COPY requirements.txt /tmp/publisher-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/publisher-requirements.txt

ENV TZ=Asia/Tokyo \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
