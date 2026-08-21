# Rootless Docker per-WorkItem execution contract

This document records the Runner isolation boundary. The repository contains a fail-closed,
explicit `rootless_docker` configuration path, fixed-argv planner, per-WorkItem session binding, and
offline fake-Docker integration tests. The dedicated Fixture Runner has been switched to this mode,
but live admission remains incomplete and the Dispatcher timer remains disabled until one successful
container Turn and its recovery checks pass. The offline example configuration is deliberately
non-deployable until its zero digest is replaced by an independently reviewed image digest.

Rootless Docker is preferred over a rootful daemon because both the daemon and containers run in a
user namespace without host root privileges. The target account must never join a `docker` group or
receive access to `/var/run/docker.sock`. Follow Docker's official
[rootless mode prerequisites](https://docs.docker.com/engine/security/rootless/): `newuidmap`,
`newgidmap`, a non-overlapping subordinate UID/GID range of at least 65,536, cgroup v2 with systemd,
and a user service with linger enabled. Do not use the convenience installation script in the
production path; install version-pinned packages from the reviewed official Ubuntu repository.

The rootless daemon socket is a host-only control interface. The fixed Runner process may address
only an absolute `unix:///run/user/<uid>/docker.sock` URL. No socket, Docker configuration, image
build context, SSH agent, Control Host file, or other WorkItem is mounted into a task container.
The production daemon uses a root-owned, protected XDG Docker `daemon.json` with `"group":"root"`.
Namespace root then maps the socket group to the locked `codex-runner` host account instead of a
subordinate GID. A root-owned systemd user-service drop-in fixes `XDG_CONFIG_HOME` and `UMask=0077`;
the runtime directory and every parent remain protected, socket owner and group must both equal the
Runner UID/GID, and other permission bits must be zero. The observed socket mode is `1660`.

## Fixed container boundary

Every Turn plan uses:

- an immutable lowercase image reference pinned by `@sha256:<64 hex>` and `--pull=never`;
- one deterministic container name bound to the validated Turn ID;
- `--rm`, `--log-driver=none`, interactive standard input without a TTY, and no detached or restart
  mode;
- explicit container UID/GID `0:0`; under the dedicated rootless daemon this maps to the
  `codex-runner` host UID/GID and prevents an image-level `USER` from selecting a subordinate host
  UID outside the existing Runner-UID firewall rule;
- a read-only root filesystem and a bounded mode-`1777`, `nosuid`, `nodev` `/tmp` tmpfs;
- `--cap-drop=ALL`, `no-new-privileges=true`, Docker's default seccomp profile, no privileged mode,
  no host PID/network/user namespace, no devices, and no added capabilities;
- hard limits of 2 CPUs, 8 GiB memory with no extra swap, 512 PIDs, bounded file descriptors,
  processes, core files, and the existing application timeout. A fixed in-container GNU `timeout`
  kills Codex before the longer host-side Docker-client deadline; if the host deadline is ever hit,
  the durable Turn remains `executing`/unknown for STATUS reconciliation rather than claiming a
  clean failure;
- the dedicated `codex-egress` network, which is only a name until live inspection proves its
  firewall behavior;
- only five bind mounts for a Turn: the root-owned digest-verified Codex executable read-only, that
  WorkItem's `repo/` read-write, that WorkItem's dedicated Codex session home read-write, the
  protected Runner auth file read-only, and the root-owned output Schema read-only;
- the Prompt only on standard input and no Docker or Codex argv derived from Issue text.

The authentication check receives only the WorkItem session home and read-only auth file; it does
not receive the repository or Schema. Docker output is never persisted by the daemon because the
Runner already captures it through a bounded pipe and reduces it to the strict Agent result.
The host Docker CLI receives an explicit empty, protected `DOCKER_CONFIG`; ambient `HOME`, Docker
contexts, client proxy configuration, credential helpers, and a user-selected daemon are absent.

Docker bind mounts are writable by default and directly expose host paths, so every source must be
an owned, protected, non-symlink path derived from the durable WorkItem registry. The planner's four
mounts are necessary but not sufficient: runtime integration must freshly validate source ownership,
mode, type, resolved containment, and the per-WorkItem auth mount behavior immediately before
starting Docker. See Docker's official
[bind-mount security warning](https://docs.docker.com/engine/storage/bind-mounts/).

Docker has no resource limits by default, so every hard limit above is mandatory and must be proven
through the container cgroup rather than inferred from argv. See the official
[resource constraints](https://docs.docker.com/engine/containers/resource_constraints/) and
[`docker container run` reference](https://docs.docker.com/reference/cli/docker/container/run).

## Authentication and session migration gate

The current direct Runner has one shared `/srv/codex-runner/app` containing ChatGPT authentication
and all existing session state. Mounting that complete directory into every container would preserve
behavior but would not isolate WorkItems, so it is forbidden.

The container layout gives each WorkItem a separate protected `runner-state/codex-home`. Its exact
WorkItem/session/image/Codex-binary-digest binding is stored in
`runner-state/codex-session.json`, which is never mounted into the container. START requires an
absent binding and an absent or empty session home. RESUME requires the exact protected session,
image, and binary binding; legacy shared-home state is therefore blocked rather than silently
replaced. Only the minimum auth file is supplied read-only for the duration of the container.
Offline fake execution now proves the START/RESUME identity and failure boundary. A live
credential-safe fixture must still prove:

1. `codex login status` succeeds without mutating the protected auth source;
2. token refresh does not require a writable shared auth file or silently invalidate the source;
3. a new session persists only in the selected WorkItem home and resumes there by exact session ID;
4. one container cannot enumerate, read, modify, or delete another WorkItem home;
5. existing direct-mode WorkItems migrate their exact session state only after independently
   verified counts and IDs, followed by an exact host-side binding record; without both, RESUME is
   rejected and must never create a replacement session.

No credential value, session content, Prompt, raw JSONL stream, or full container output may be
printed, logged, copied to GitHub/Slack, or committed during these proofs.

## Network and disk gates

A Docker network name is not an egress policy. Before activation, independent probes must prove that
the selected `codex-egress` implementation allows only the required public Codex/dependency path and
denies host loopback, RFC1918/ULA, link-local, cloud metadata, Control Host, Docker APIs, and other
containers. Docker's [`none` network](https://docs.docker.com/engine/network/drivers/none/) is the
safe negative-control test but cannot run Codex by itself. Firewall, routing, DNS proxy, and metadata
rules are separate host infrastructure changes and require exact-command approval and rollback.
The selected unified HTTP CONNECT proxy, protected allowlist, metadata-only audit format, Runner-UID
firewall boundary, and guarded rollback are specified in [EGRESS.md](EGRESS.md). The host-level
Runner path on `s3` has passed the native parser, allow/deny, direct-egress, private/metadata,
fail-closed, audit, and rotation probes. That result does not prove the future rootless container
path: its container-visible proxy endpoint and no-bypass firewall behavior still require separate
live acceptance with a credential-free image. The protected Docker config accepts only the intended
container-visible `http://10.0.2.2:3128` endpoint and the exact
`unix:///run/user/<runner-uid>/docker.sock`; these structural checks are not substitutes for the
live probes.

Immediately before each authentication or Turn container, the Runner reads back both Docker assets
through the exact rootless socket and empty CLI configuration. The image must expose only the exact
configured RepoDigest and report `linux/amd64`. The `codex-egress` network must be a local bridge on
`172.30.0.0/24` with gateway `172.30.0.1`, IPv6/internal/attachable/ingress disabled, inter-container
communication disabled, masquerading enabled, and no already attached container. Any missing,
additional, or drifted identity fails before the Prompt enters Docker. These structural checks still
require the live proxy, direct-bypass, private/metadata, and second-container probes below.

The current `s3` root filesystem is ext4 without project quotas. Rootless mode therefore provisions
one preallocated ext4 image per new WorkItem outside `work_items_root` and mounts it with `fuse2fs`.
The protected configuration fixes the image byte limit, a separate host-free-space reserve, and all
filesystem helper paths. PREPARE holds the global Runner lock; a new image is formatted and populated
only through the fixed `mkfs.ext4 -q -F -m 0 -E nodiscard -L codex-work-item <image>` argv and a
deterministic staging mount, cleanly unmounted and checked before atomic publication,
then remounted at the exact WorkItem root. Every later operation verifies the image is a single-link,
fully allocated, owned mode-`0600` regular file of the exact size and verifies the live mount's exact
source, target, `fuse.ext4` type, Runner UID/GID, `rw`, `nosuid`, `nodev`, and bounded capacity.
Incomplete staging state is preserved and blocks retry rather than being deleted or guessed. A
legacy direct-mode WorkItem has no matching image and is therefore not silently admitted or migrated.
`nodiscard` is mandatory: default ext4 formatting may punch holes in a preallocated regular backing
file, invalidating the dense-allocation invariant and the host-reserve admission proof.

The image-size check is a real per-WorkItem hard byte limit; the separately preallocated host reserve
prevents admission from intentionally consuming the final protected capacity. Neither replaces live
exhaustion, clean-unmount, filesystem-check, remount, restart, and recovery acceptance on the target.
A free-space preflight alone remains only an admission/alert control, not isolation.

## Live acceptance boundary

Before the first container Turn:

1. complete and live-verify the dedicated `codex-runner` account and root-owned SSH/release boundary;
2. stop and disable the Control Host Dispatcher timer and prove no active local or remote Turn;
3. create and verify a fresh Control Host SQLite Online Backup;
4. install version-pinned rootless Docker packages and a digest-pinned reviewed image without using
   a convenience script;
5. prove rootless/security/cgroup status, no rootful daemon/socket, exact mounts, no Docker socket,
   limits, read-only rootfs, capabilities, seccomp, network denial, disk exhaustion, and auth/session
   behavior with a credential-free image first;
6. run one dedicated private Fixture WorkItem, read back SQLite/GitHub/Runner/Slack/Actions, and prove
   an immediate repeated sweep is idle;
7. keep higher-value repositories prohibited until attack and recovery acceptance is complete.

Rollback keeps the Dispatcher timer disabled, stops the rootless user daemon, restores the previous
Runner release/config/account binding, and uses read-only STATUS reconciliation. Preserve every
WorkItem directory, session home, Turn record, SQLite backup, branch, and PR. Do not delete a
container, image, volume, session, or namespace to resolve ambiguous state.
