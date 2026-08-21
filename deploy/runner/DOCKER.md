# Rootless Docker per-WorkItem execution contract

This document records the Runner isolation boundary. The repository contains a fail-closed,
explicit `rootless_docker` configuration path, fixed-argv planner, per-WorkItem session binding, and
offline fake-Docker integration tests. The dedicated Fixture Runner has been switched to this mode,
and a dedicated private Fixture has now passed one successful container RESUME Turn plus independent
SQLite, Runner, GitHub, Slack, Actions, and repeated-idle read-back. The Dispatcher timer remains
disabled and higher-value repositories remain prohibited until the remaining attack and recovery
acceptance is complete. The offline example configuration is deliberately non-deployable until its
zero digest is replaced by an independently reviewed image digest.

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
- only five bind mounts for a Turn: the independently digest-verified root-owned `codex` and
  `codex-code-mode-host` executables read-only, that WorkItem's `repo/` read-write, that WorkItem's
  dedicated Codex session/auth home read-write, and the root-owned output Schema read-only;
- the Prompt only on standard input and no Docker or Codex argv derived from Issue text.

The authentication check receives the two read-only executables and only the WorkItem session/auth
home; it does not receive the repository, Schema, Runner-wide auth source, or host binding record.
Docker output is never persisted by the daemon because the Runner already captures it through a
bounded pipe and reduces it to the strict Agent result.
The host Docker CLI receives an explicit empty, protected `DOCKER_CONFIG`; ambient `HOME`, Docker
contexts, client proxy configuration, credential helpers, and a user-selected daemon are absent.

The reviewed production image must match the allowed repository workload rather than inherit the
broad universal development image indefinitely. The current Python/npm Web baseline is defined in
[`image/Dockerfile`](image/Dockerfile): Python 3.12, Node.js 22/npm, and a small fixed set of shell,
Git, HTTP, search, patch, and timeout tools. Other language runtimes, build systems, browsers,
privilege tools, and Docker clients are absent. Projects that need native compilation require a
separately reviewed image variant; they must not install an unbounded toolchain into this baseline.

Docker bind mounts are writable by default and directly expose host paths, so every source must be
an owned, protected, non-symlink path derived from the durable WorkItem registry or the fixed Codex
tool bundle. The Turn planner's five mounts are necessary but not sufficient: runtime integration
must freshly validate source ownership, mode, type, resolved containment, independent executable
digests, and the per-WorkItem auth mount behavior immediately before starting Docker. See Docker's
official
[bind-mount security warning](https://docs.docker.com/engine/storage/bind-mounts/).

Docker has no resource limits by default, so every hard limit above is mandatory and must be proven
through the container cgroup rather than inferred from argv. See the official
[resource constraints](https://docs.docker.com/engine/containers/resource_constraints/) and
[`docker container run` reference](https://docs.docker.com/reference/cli/docker/container/run).

## Authentication and session migration gate

The Runner keeps one protected `/srv/codex-runner/app/auth.json` only as the host seed. Mounting the
complete Runner-wide home or that shared file read-write into every container is forbidden.

The container layout gives each WorkItem a separate protected `runner-state/codex-home`. Before its
first container command, the host copies the seed once to that home's mode-`0600` `auth.json`. The
WorkItem home is the writable mount, so Codex may atomically refresh its own auth file without a
replace-hostile nested file mount. The immutable `runner-state/codex-auth-binding.json` remains
outside every container mount and binds the WorkItem to the seed digest without exposing credential
content. The Runner-wide seed is never mounted and must remain byte-identical. Other WorkItems
receive different files and cannot enumerate, replace, or delete this one.

The exact WorkItem/session/image/primary-Codex binding remains in the backward-compatible
`runner-state/codex-session.json`; the independently hashed companion binding is stored in the new
`runner-state/codex-session-tools.json` sidecar. Neither file is mounted into the container. START
requires all bindings to be absent and the session home to be absent or empty. RESUME requires the
exact protected session, image, executable, and auth bindings. An existing isolated session without
the tool or auth sidecar may gain each once only after its original session binding matches and the
current host inputs pass their protected-path checks. The original version-1 session binding is
never rewritten, so the previous Runner can still read it. If the WorkItem auth has diverged through
refresh, however, rolling back to a release that uses the Runner-wide seed is not a safe RESUME path.
Conflicting, partial, missing-after-binding, legacy shared-home, malformed, linked, oversized, or
weakly protected auth state is rejected before Codex starts.
Offline fake execution proves the START/RESUME identity and failure boundary. Live acceptance must
prove:

1. `codex login status` succeeds without mutating the protected auth source;
2. token refresh can atomically update only the selected WorkItem auth file without mutating or
   silently invalidating the Runner-wide seed;
3. a new session persists only in the selected WorkItem home and resumes there by exact session ID;
4. one container cannot enumerate, read, modify, or delete another WorkItem home;
5. existing direct-mode WorkItems migrate their exact session state only after independently
   verified counts and IDs, followed by an exact host-side binding record; without both, RESUME is
   rejected and must never create a replacement session.

The dedicated Issue `#24` recovery fixture proved items 1, 3, and 5 for one existing isolated
session: the original version-1 binding remained byte-identical, the companion sidecar was added
only after authentication and executable validation, and Turn 2 resumed the same session and
produced one checkpoint. It did not intentionally force a token refresh, so item 2 remains a
version-specific operational risk. The WorkItem auth host binding and atomic-refresh behavior are
covered offline but require a new dedicated Fixture before deployment admission. Cross-WorkItem
denial and the network/disk boundaries were proved separately with credential-free probes; repeat
them whenever those boundaries change.

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
firewall boundary, and guarded rollback are specified in [EGRESS.md](EGRESS.md). The host and
rootless-container paths on `s3` have passed the credential-free parser, allow/deny, direct-bypass,
private/metadata, fail-closed, audit, and rotation probes. Those observations are version-specific
and must be repeated after proxy, firewall, Docker, image, or network changes. The protected Docker
config accepts only the intended container-visible `http://10.0.2.2:3128` endpoint and the exact
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

## Live acceptance boundary and current status

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

Items 1 through 6 have passed for the dedicated private Fixture, including the successful Issue
`#24` RESUME recorded in `docs/live-test-evidence.md`. Item 7 remains in force. The Dispatcher timer
is intentionally disabled while the operator reviews this checkpoint; successful Fixture admission
does not authorize unattended use for another repository class.

### Remaining admission matrix

| Gate | Fixture-only unattended status | Higher-value repository status |
|---|---|---|
| Rootless daemon, image, mounts, cgroups, proxy and per-WorkItem disk | Live-proved; repeat after any relevant asset change | Requires the same exact target read-back |
| Per-WorkItem writable auth plus host-only binding | Offline candidate; one new START/RESUME Fixture and seed/file digest comparison required | Blocked until that live proof passes |
| Natural token refresh | The layout permits isolated atomic replacement; never force expiry by editing a credential | Blocked until a version-specific refresh/rotation procedure preserves the seed and other WorkItems |
| Runner/client timeout or process loss | Durable `executing` becomes unknown and blind replay is forbidden | Blocked until operator recovery/abandonment semantics are explicitly accepted for the repository |
| Docker/host restart with no active Turn | Credential-free restart/remount probes passed | Must be repeated after the final auth/runtime release |
| Backup publication | Integrity and atomic publication passed; temporary SQLite sidecars must also be absent | Same requirement plus a restore drill |

Fixture-only timer activation may proceed only after the offline candidate is independently deployed,
one dedicated Fixture proves the WorkItem auth binding without exposing its contents, the backup
sidecar fix is live-verified, temporary administrative access is removed, and preflight plus a
repeated sweep are idle. It does not satisfy the higher-value column.

Rollback keeps the Dispatcher timer disabled, stops the rootless user daemon, restores the previous
Runner release/config/account binding, and uses read-only STATUS reconciliation. Preserve every
WorkItem directory, session home, Turn record, SQLite backup, branch, and PR. Do not delete a
container, image, volume, session, or namespace to resolve ambiguous state.
