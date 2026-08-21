# Minimal Runner Web image

This image replaces the broad `codex-universal` environment for the current Python/npm Web scope.
It deliberately contains only Python 3.12, pip, Node.js 22, npm/npx, Bash, Git, CA certificates,
curl, GNU `timeout`, ripgrep, patch, and their runtime libraries. It has no compiler toolchain,
Java, Go, Rust, Ruby, PHP, .NET, Swift, Bun, Gradle/Maven, browser, Docker CLI, or `sudo`.

The host's independently hashed, static Codex CLI and its fixed sibling
`codex-code-mode-host` remain separate read-only bind mounts at `/usr/local/bin/codex` and
`/usr/local/bin/codex-code-mode-host`; neither is copied into the image. Repository, WorkItem
session home, authentication, output Schema, network, resource, and read-only-root boundaries
remain owned by the Runner invocation rather than this Dockerfile.

Both Docker Official Image inputs are versioned and digest-pinned. The Node stage contributes only
the Node executable, npm/npx module, and Node license to the final Python image. Docker recommends
[minimal bases, multi-stage builds, and digest pinning](https://docs.docker.com/build/building/best-practices/).
Base-digest updates require a new review, build, package inventory, vulnerability report, and final
runtime digest; a floating tag must never enter Runner configuration.

The Node 22.23.2 release still bundles npm 10.9.8 with fixable high and critical findings in npm's
own dependency tree. The build therefore pins the Node-22-compatible npm 11.19.0 release and stages
two exact patch-level transitive updates, `brace-expansion@5.0.9` and `ip-address@10.3.1`, in an empty
temporary prefix before replacing only those packages and `balanced-match@4.0.4` in npm's dependency
tree. The build and runtime gates verify those versions plus the expected `picomatch@4.0.4`,
`sigstore@4.1.1`, and `tar@7.5.19` versions. These overrides must be removed or updated only after a
new independently scanned npm release contains the same or newer fixes.
The final stage also rewrites the fixed Debian mirror entries to HTTPS before the first package
operation; the audited build proxy never needs to permit plaintext HTTP.

## Build gate

Build only for `linux/amd64` on an isolated reviewed builder. Do not pass secrets, auth files, SSH
agents, Docker configuration, Runner paths, or Issue content as build arguments or context. Record:

- the two resolved base manifest digests;
- the final image ID and exact pushed RepoDigest;
- `docker image inspect` OS/architecture, configuration, uncompressed size, and layer count;
- sorted `dpkg-query` and npm version output without environment or credential values;
- a vulnerability report that has no fixable critical or high finding;
- the exact builder release and the Dockerfile commit.

The final image must be at most 1 GiB uncompressed and its complete rootless-Docker storage increase,
after removing build cache only, must be at most 2 GiB. A larger result fails closed.

## Credential-free acceptance

Before any auth mount or Prompt enters the image, prove with the production fixed Runner argv:

1. Python reports 3.12; Node reports major 22; pip, npm/npx, Git, curl, `rg`, patch, and GNU `timeout`
   execute; every deliberately excluded tool remains absent.
2. The root filesystem is read-only; only the bounded `/tmp` tmpfs and one WorkItem filesystem are
   writable; capabilities are empty, `NoNewPrivs` and seccomp are active, and CPU/memory/PID/swap
   limits match the Runner contract.
3. The Docker socket, host paths, other WorkItems, and ambient Docker/SSH configuration are absent.
4. Public access works only through the audited proxy; direct, unrelated public, private, metadata,
   host, and other-container access fail; stopping the proxy fails closed.
5. The exact host Codex and code-mode-host binary digests run through separate read-only mounts,
   while no credential, Prompt, raw stream, or complete output is printed or persisted outside the
   existing bounded protocol.

Push the candidate to the dedicated private GHCR package, pull it by RepoDigest, and configure only
that lowercase `name@sha256:<digest>` identity. A locally built tag or image ID is insufficient
because the Runner requires exactly one matching `RepoDigests` entry.

The repository workflow separates review builds from publication. Pull requests receive only
`contents: read`; they never receive package write permission. Publication is a manual dispatch from
`main` and requires the operator to enter the exact 40-character commit. It grants `packages: write`
only to the publication job, publishes only `sha-<commit>` (never `latest`), pulls back the exact
RepoDigest, and removes its ephemeral registry configuration in an `always()` step. The first GHCR
version is expected to remain private. Publishing a candidate does not admit it to the Runner: the
vulnerability, fixed-argv isolation, storage, recovery, and live Fixture gates above still apply.

Keep the current `codex-universal` digest throughout credential-free probes, the dedicated private
Fixture Turn, STATUS/recovery, SQLite/GitHub/Runner/Slack/Actions readback, and repeated idle sweep.
Only after those checks pass may an explicitly approved `docker image rm` target the exact old
RepoDigest. Never use `docker system prune`, `docker image prune`, a name pattern, or a bulk delete.

Projects that require native npm modules or Python packages without compatible wheels must fail
closed. Add a narrowly reviewed build-tool variant later instead of installing compilers at runtime
or widening this base implicitly.
