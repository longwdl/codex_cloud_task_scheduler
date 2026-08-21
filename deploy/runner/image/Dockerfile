FROM --platform=linux/amd64 docker.io/library/node:22.23.1-trixie-slim@sha256:e6d9a389d34ff9678438af985c9913fbd1eb6ed36e80fea56644f4b4f6dd70ba AS node_runtime

FROM --platform=linux/amd64 docker.io/library/python:3.12.13-slim-trixie@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

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
    /usr/local/bin/npm --version; \
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
