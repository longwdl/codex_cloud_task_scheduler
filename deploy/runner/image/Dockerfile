FROM --platform=linux/amd64 docker.io/library/node:22.23.2-trixie-slim@sha256:f4c1b09232a0ae8f765093968ec82107a1be65cb0bfb36fc831195794f139568 AS node_runtime

RUN set -eux; \
    /usr/local/bin/npm install --global --ignore-scripts --no-audit --no-fund \
        --cache=/tmp/npm-cache npm@11.19.0; \
    /usr/local/bin/npm install \
        --prefix=/tmp/npm-hotfix \
        --ignore-scripts \
        --no-audit \
        --no-fund \
        --no-save \
        --cache=/tmp/npm-cache \
        brace-expansion@5.0.9 \
        ip-address@10.3.1; \
    test "$(/usr/local/bin/node -p 'require("/tmp/npm-hotfix/node_modules/balanced-match/package.json").version')" = 4.0.4; \
    rm -rf \
        /usr/local/lib/node_modules/npm/node_modules/balanced-match \
        /usr/local/lib/node_modules/npm/node_modules/brace-expansion \
        /usr/local/lib/node_modules/npm/node_modules/ip-address; \
    cp -a /tmp/npm-hotfix/node_modules/balanced-match /usr/local/lib/node_modules/npm/node_modules/; \
    cp -a /tmp/npm-hotfix/node_modules/brace-expansion /usr/local/lib/node_modules/npm/node_modules/; \
    cp -a /tmp/npm-hotfix/node_modules/ip-address /usr/local/lib/node_modules/npm/node_modules/; \
    rm -rf /tmp/npm-cache /tmp/npm-hotfix; \
    test "$(/usr/local/bin/npm --version)" = 11.19.0; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/balanced-match/package.json").version')" = 4.0.4; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/brace-expansion/package.json").version')" = 5.0.9; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/ip-address/package.json").version')" = 10.3.1; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/tinyglobby/node_modules/picomatch/package.json").version')" = 4.0.4; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/sigstore/package.json").version')" = 4.1.1; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/tar/package.json").version')" = 7.5.19

FROM --platform=linux/amd64 docker.io/library/python:3.12.14-slim-trixie@sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4

LABEL org.opencontainers.image.title="codex-cloud-task-scheduler-runner" \
      org.opencontainers.image.description="Minimal Python and npm environment for the isolated Codex Runner" \
      org.opencontainers.image.source="https://github.com/longwdl/codex_cloud_task_scheduler"

ENV LANG=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=node_runtime /usr/local/LICENSE /usr/local/LICENSE.node

RUN set -eux; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    grep -F 'URIs: https://deb.debian.org/' /etc/apt/sources.list.d/debian.sources; \
    ! grep -F 'URIs: http://' /etc/apt/sources.list.d/debian.sources; \
    ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        libatomic1 \
        patch \
        ripgrep; \
    rm -rf /var/lib/apt/lists/* /tmp/npm-cache; \
    test "$(/usr/local/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = 3.12; \
    test "$(/usr/local/bin/node -p 'process.versions.node.split(".")[0]')" = 22; \
    /usr/local/bin/python3 -m pip --version; \
    test "$(/usr/local/bin/npm --version)" = 11.19.0; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/balanced-match/package.json").version')" = 4.0.4; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/brace-expansion/package.json").version')" = 5.0.9; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/ip-address/package.json").version')" = 10.3.1; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/tinyglobby/node_modules/picomatch/package.json").version')" = 4.0.4; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/sigstore/package.json").version')" = 4.1.1; \
    test "$(/usr/local/bin/node -p 'require("/usr/local/lib/node_modules/npm/node_modules/tar/package.json").version')" = 7.5.19; \
    /usr/bin/git --version; \
    /usr/bin/curl --version; \
    /usr/bin/rg --version; \
    /usr/bin/patch --version; \
    /usr/bin/timeout --version; \
    for forbidden in bun cargo dotnet gcc g++ go gradle java javac make mvn php ruby rustc sudo swift; do \
        ! command -v "$forbidden"; \
    done

WORKDIR /workspace

ENTRYPOINT []
CMD ["/bin/bash"]
