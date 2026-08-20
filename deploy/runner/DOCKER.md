# Rootless Docker per-WorkItem execution contract

This document records the next Runner isolation boundary. The repository currently contains only
the pure fixed-argv planner and offline invariants; it does not yet switch `RunnerTurnExecutor` to
Docker, install an engine, build an image, migrate existing Codex sessions, or authorize a live
container Turn.

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

## Fixed container boundary

Every Turn plan uses:

- an immutable lowercase image reference pinned by `@sha256:<64 hex>` and `--pull=never`;
- one deterministic container name bound to the validated Turn ID;
- `--rm`, `--log-driver=none`, interactive standard input without a TTY, and no detached or restart
  mode;
- a read-only root filesystem and a bounded mode-`1777`, `nosuid`, `nodev` `/tmp` tmpfs;
- `--cap-drop=ALL`, `no-new-privileges=true`, Docker's default seccomp profile, no privileged mode,
  no host PID/network/user namespace, no devices, and no added capabilities;
- hard limits of 2 CPUs, 8 GiB memory with no extra swap, 512 PIDs, bounded file descriptors,
  processes, core files, and the existing application timeout;
- the dedicated `codex-egress` network, which is only a name until live inspection proves its
  firewall behavior;
- only four bind mounts for a Turn: that WorkItem's `repo/` read-write, that WorkItem's dedicated
  Codex session home read-write, the root-owned Runner auth file read-only, and the root-owned output
  Schema read-only;
- the Prompt only on standard input and no Docker or Codex argv derived from Issue text.

The authentication check receives only the WorkItem session home and read-only auth file; it does
not receive the repository or Schema. Docker output is never persisted by the daemon because the
Runner already captures it through a bounded pipe and reduces it to the strict Agent result.

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

The intended container layout gives each WorkItem a separate protected session home below its own
`runner-state/`. Only the minimum auth file is supplied read-only for the duration of the container.
Before wiring this mode, an offline fake and a live credential-safe fixture must prove:

1. `codex login status` succeeds without mutating the root-owned auth source;
2. token refresh does not require a writable shared auth file or silently invalidate the source;
3. a new session persists only in the selected WorkItem home and resumes there by exact session ID;
4. one container cannot enumerate, read, modify, or delete another WorkItem home;
5. existing direct-mode WorkItems either migrate their exact session state with independently
   verified counts and IDs or remain explicitly blocked; they must never receive a replacement
   session automatically.

No credential value, session content, Prompt, raw JSONL stream, or full container output may be
printed, logged, copied to GitHub/Slack, or committed during these proofs.

## Network and disk gates

A Docker network name is not an egress policy. Before activation, independent probes must prove that
the selected `codex-egress` implementation allows only the required public Codex/dependency path and
denies host loopback, RFC1918/ULA, link-local, cloud metadata, Control Host, Docker APIs, and other
containers. Docker's [`none` network](https://docs.docker.com/engine/network/drivers/none/) is the
safe negative-control test but cannot run Codex by itself. Firewall, routing, DNS proxy, and metadata
rules are separate host infrastructure changes and require exact-command approval and rollback.

The current `s3` root filesystem is ext4 without project quotas. CPU, memory, PID, tmpfs, and timeout
limits therefore do not provide a per-WorkItem aggregate disk limit for the bind-mounted repository.
Do not claim Phase F complete until a separately reviewed loopback filesystem, project-quota-capable
filesystem, or equivalent hard byte limit is created and tested for exhaustion, cleanup, restart,
and recovery. A free-space preflight alone is only an admission/alert control, not isolation.

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
