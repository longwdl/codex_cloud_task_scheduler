# Current-schema disaster recovery and exact Runner asset reclamation

The command retains its historical `schema18-disaster-recovery` name, but the current release
requires the exact schema-21 migration set (1 through 21).

This runbook has two independent workflows. The disaster-recovery drill may write only below a new
isolated recovery directory and may perform GitHub and Slack reads. Reclamation planning is
read-only except for immutable plan/inventory files. Actual reclamation is a separate destructive
operation and must never follow automatically from a plan.

## Measured recovery boundary

The receipt's `rto_milliseconds` measures one exact interval: complete offline bundle validation
through isolated schema-21 restore, release and operational-handoff receipt validation,
Control/Runner version agreement, Runner registry/archive/absence and schema-v2 reference
reconciliation, GitHub Issue/PR/branch read-back, Slack permalink read-back, and reconstruction of
empty Control and Runner application filesystem roots. The receipt records the backup age as the
observed recovery point.

This is not a claim about VM procurement, OS installation, DNS, package mirrors, secret-manager
availability, or operator approval latency. Those infrastructure intervals are explicitly marked
unmeasured. The isolated empty roots do not replace either live `current` symlink and never start a
service. They contain the exact release, protected Control config, systemd units, restored
database, permanent handoff/release receipts, Runner reference ledger/apply receipt, and strict
planner status needed to prove both application reconstruction paths.

## Schema-20 isolated drill

Preconditions:

- leave the current Control and Runner environments in place;
- require a committed schema-v2 release receipt for the exact current commit;
- require a permanent operational handoff receipt for that release;
- require the newest retained backup to be mode `0600`, schema 21, integral, and free of foreign-key
  violations;
- require no unfinished Slack outbox row;
- place copied inputs and the bundle in `codex-dispatcher`-owned mode-`0700` directories and never
  print the protected configuration or provider credentials;
- include each isolated Control database that owns a Runner-only terminal WorkItem and the permanent
  reclamation-canary receipt whose two messages must be read back from the system channel.

On the Runner, collect one bounded schema-v3 recovery snapshot as root. It includes WorkItem
registry/tombstones, the exact current/rollback release and image ledger, its immutable apply
receipt, the latest planner status, installed planner-unit hashes, and explicit repository/Issue
identity for every registry. Replace the commit with the exact value returned by
`readlink /srv/codex-runner/current`:

```bash
sudo /srv/codex-runner/bin/codex-runner-maintenance-v1 \
  recovery-snapshot --current-release-commit <40-hex-current-commit> \
  > /var/tmp/runner-recovery-<40-hex-current-commit>.json
```

Copy that JSON to the Control Host, then prepare immutable inputs without changing the live state:

```bash
sudo install -d -o codex-dispatcher -g codex-dispatcher -m 0700 \
  /var/lib/codex-dispatcher/disaster-recovery-inputs \
  /var/lib/codex-dispatcher/disaster-recovery-bundles \
  /var/lib/codex-dispatcher/disaster-recovery-drills
sudo install -o codex-dispatcher -g codex-dispatcher -m 0600 \
  /var/tmp/runner-recovery-<40-hex-current-commit>.json \
  /var/lib/codex-dispatcher/disaster-recovery-inputs/runner-<40-hex-current-commit>.json
sudo install -o codex-dispatcher -g codex-dispatcher -m 0600 \
  /opt/codex-dispatcher/release-receipts/<40-hex-current-commit>.json \
  /var/lib/codex-dispatcher/disaster-recovery-inputs/release-<40-hex-current-commit>.json
sudo install -o codex-dispatcher -g codex-dispatcher -m 0600 \
  /opt/codex-dispatcher/release-handoff-receipts/<40-hex-current-commit>.json \
  /var/lib/codex-dispatcher/disaster-recovery-inputs/handoff-<40-hex-current-commit>.json
```

Create one new bundle through a transient service. The bundle includes the protected Control
configuration, so its directory and every non-release artifact remain mode `0700`/`0600`. The
source canary database below is the retained schema-21 backup that owns the three higher-value
WorkItems; replace paths only with exact reviewed equivalents:

```bash
sudo systemd-run --wait --collect --pipe \
  --property=User=codex-dispatcher --property=Group=codex-dispatcher \
  --property=WorkingDirectory=/var/lib/codex-dispatcher \
  --setenv=HOME=/var/lib/codex-dispatcher/home \
  --setenv=PYTHONPATH=/opt/codex-dispatcher/current/src \
  --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=PYTHONNOUSERSITE=1 \
  /opt/codex-python/current/bin/python3 -P -s -m codex_dispatcher \
  disaster-recovery-bundle-create \
  --config /etc/codex-dispatcher/config.toml \
  --bundle-root /var/lib/codex-dispatcher/disaster-recovery-bundles/<UTC-bundle-id> \
  --control-release /opt/codex-dispatcher/releases/<40-hex-current-commit> \
  --release-receipt /var/lib/codex-dispatcher/disaster-recovery-inputs/release-<40-hex-current-commit>.json \
  --handoff-receipt /var/lib/codex-dispatcher/disaster-recovery-inputs/handoff-<40-hex-current-commit>.json \
  --runner-snapshot /var/lib/codex-dispatcher/disaster-recovery-inputs/runner-<40-hex-current-commit>.json \
  --provenance-database /var/lib/codex-dispatcher/higher-value-canary/backups/state-pre-discard-20260824T040611Z.db \
  --system-slack-receipt /var/lib/codex-dispatcher/reclamation-canaries/<fixture-id>/receipt.json \
  --create --json
```

Export the completed bundle to storage outside both Linux hosts. This command runs from the trusted
operator workstation, transfers opaque bytes, and does not display the protected config:

```bash
install -d -m 0700 <off-host-bundle-parent>
ssh s2 sudo tar -C /var/lib/codex-dispatcher/disaster-recovery-bundles \
  -cf - <UTC-bundle-id> | tar -C <off-host-bundle-parent> -xf -
```

Keep `<off-host-bundle-parent>` mode `0700` and preserve the release tree's executable/read modes;
do not recursively normalize artifact modes after export.

For a real drill, copy the off-host bundle back into a new protected Control path and validate that
copy. Do not reuse the on-host source bundle as evidence of independent recovery. Run the drill
through a transient service so the protected environment file is read without displaying tokens.
`<UTC-run-id>` must name a directory that does not already exist:

```bash
sudo systemd-run --wait --collect --pipe \
  --property=User=codex-dispatcher --property=Group=codex-dispatcher \
  --property=WorkingDirectory=/var/lib/codex-dispatcher \
  --property=EnvironmentFile=/etc/codex-dispatcher/dispatcher.env \
  --setenv=HOME=/var/lib/codex-dispatcher/home \
  --setenv=PYTHONPATH=/opt/codex-dispatcher/current/src \
  --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=PYTHONNOUSERSITE=1 \
  /opt/codex-python/current/bin/python3 -P -s -m codex_dispatcher \
  schema18-disaster-recovery \
  --config /etc/codex-dispatcher/config.toml \
  --recovery-root /var/lib/codex-dispatcher/disaster-recovery-drills/<UTC-run-id> \
  --bundle /var/lib/codex-dispatcher/disaster-recovery-bundles/<reimported-bundle-id> \
  --execute-isolated --json
```

Acceptance requires `status=passed`, `database_schema_migrations=[1,...,20]`, identical Control and
Runner commits, exact reference and planner digests, all recorded external counts reconciled,
`online_state_modified=false`, distinct Control/Runner rebuild-manifest SHA-256 values, and a
mode-protected `receipt.json`. A failure writes `failed-receipt.json`; retain it and the source
backup, and remove only that exact recovery directory after investigation. Do not retry by reusing
the same recovery directory.

Runner terminal evidence may include canary WorkItems that were intentionally never inserted into
the online Control database. Such an identity is accepted only when an included, integral schema-21
canary database binds the same WorkItem, repository, Issue, and published terminal HEAD. The receipt
reports these separately as `runner_orphan_terminal_count`. A tombstone without that database
provenance, an extra provenance row, an overlapping archive/absence, or a mismatch for an online
Control WorkItem fails closed.

## Empty Control Host recovery commands

The drill exercises these application steps under `empty-control-host/`. On a genuinely new Linux
host, provision the pinned Python, GitHub CLI, SSH client, service account, SSH keys, known-hosts,
and protected secret environment first. Their elapsed time is outside the measured application
RTO. Keep every Dispatcher timer disabled while running the following sequence:

```bash
sudo systemctl disable --now codex-dispatcher.timer codex-dispatcher-health.timer || true
sudo install -d -o root -g root -m 0755 /opt/codex-dispatcher/releases
sudo install -d -o codex-dispatcher -g codex-dispatcher -m 0700 \
  /var/lib/codex-dispatcher /var/lib/codex-dispatcher/backups
sudo install -d -o root -g root -m 0755 \
  /opt/codex-dispatcher/releases/<40-hex-release-commit>
sudo tar -C /opt/codex-dispatcher/releases/<40-hex-release-commit> \
  --no-same-owner --no-same-permissions -xf <verified-release-archive>
sudo ln -sfn releases/<40-hex-release-commit> /opt/codex-dispatcher/current.next
sudo mv -Tf /opt/codex-dispatcher/current.next /opt/codex-dispatcher/current
sudo install -o root -g codex-dispatcher -m 0640 <protected-config-copy> \
  /etc/codex-dispatcher/config.toml
sudo install -o root -g root -m 0644 \
  /opt/codex-dispatcher/current/deploy/systemd/codex-dispatcher*.service \
  /opt/codex-dispatcher/current/deploy/systemd/codex-dispatcher*.timer \
  /etc/systemd/system/
sudo -u codex-dispatcher env PYTHONPATH=/opt/codex-dispatcher/current/src \
  /opt/codex-python/current/bin/python3 -P -s -c \
  'from pathlib import Path; import sys; from codex_dispatcher.state_store import StateStore; source=Path(sys.argv[1]); target=Path(sys.argv[2]); StateStore(source, read_only=True).backup(target)' \
  <validated-current-schema-backup> /var/lib/codex-dispatcher/state.db.recovered
sudo -u codex-dispatcher env PYTHONPATH=/opt/codex-dispatcher/current/src \
  /opt/codex-python/current/bin/python3 -P -s -m codex_dispatcher status \
  --database /var/lib/codex-dispatcher/state.db.recovered --json
sudo mv /var/lib/codex-dispatcher/state.db.recovered /var/lib/codex-dispatcher/state.db
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/codex-dispatcher*.service \
  /etc/systemd/system/codex-dispatcher*.timer
```

If any check fails before the final `mv`, delete only
`state.db.recovered`. If it fails after the move but before any write-enabled sweep, move the failed
database aside and restore the same validated backup again. Once any write-enabled sweep begins,
binary-only rollback is forbidden: stop all Dispatcher timers and reconcile SQLite, Runner,
GitHub, and Slack from the durable receipts before another attempt.

## Empty Runner Host recovery commands

The drill separately exercises the application files below `empty-runner-host/`. On a genuinely
new Runner, install the pinned OS packages, rootless Docker, service account, SSH policy, Codex
login seed, egress proxy/firewall, and protected Runner config before this application layer. Keep
the Runner protocol unavailable until all hashes agree:

```bash
sudo install -d -o root -g root -m 0755 /srv/codex-runner/releases
sudo install -d -o root -g root -m 0700 \
  /srv/codex-runner/etc /srv/codex-runner/reclamation-reference-receipts
sudo tar -C /srv/codex-runner/releases/<40-hex-release-commit> \
  --no-same-owner --no-same-permissions -xf <verified-release-archive>
sudo ln -sfn releases/<40-hex-release-commit> /srv/codex-runner/current.next
sudo mv -Tf /srv/codex-runner/current.next /srv/codex-runner/current
sudo install -o root -g root -m 0600 <validated-reference-ledger> \
  /srv/codex-runner/etc/reclamation-rollback-references.json
sudo install -o root -g root -m 0600 <validated-reference-apply-receipt> \
  /srv/codex-runner/reclamation-reference-receipts/<40-hex-release-commit>.apply.json
sudo install -o root -g root -m 0644 \
  /srv/codex-runner/current/deploy/runner/codex-runner-reclamation-plan.service \
  /srv/codex-runner/current/deploy/runner/codex-runner-reclamation-plan.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/codex-runner-reclamation-plan.service \
  /etc/systemd/system/codex-runner-reclamation-plan.timer
```

Do not copy `latest.json` as authority to delete anything. Restore it only as audit evidence; run a
fresh read-only planner after Docker, WorkItem registries, tombstones, and both release trees have
been restored and re-inspected. Any reference/apply-receipt mismatch blocks Runner activation.

The planner unit retains `CAP_SETUID` and `CAP_SETGID` in its bounding set, with no ambient
capabilities, solely so
the root-owned collector can fork a one-shot inventory child and irreversibly set that child's
real, effective, and saved UID/GID to the trusted WorkItem owner before reading owner-only FUSE
session bindings. Before that transition it clears and verifies the complete supplementary-group
list; an already-unprivileged owner process is rejected if it retains any foreign supplementary
group. The trusted UID/GID must match both the WorkItem root and active-lock owner. Protected JSON
is opened with `O_NOFOLLOW`, read through one bounded descriptor, and rejected if its inode or
metadata changes during the read. The root parent revalidates the child's strict, bounded
digest/blocked-ID result before inspecting releases or writing a plan; do not replace this boundary
with `allow_other` or a broader FUSE mount policy.
On the deployed systemd version, `PrivateDevices`, `RestrictAddressFamilies`,
`RestrictNamespaces`, `RestrictSUIDSGID`, and the other seccomp-generating directives shown absent
in the checked-in unit also block the irreversible credential drop. This unit therefore replaces
`PrivateDevices` with `DevicePolicy=closed` and keeps `PrivateNetwork`. `NoNewPrivileges`, the
narrow capability set without `CAP_SYS_ADMIN`, `CAP_SYS_MODULE`, `CAP_SYSLOG`, `CAP_SYS_TIME`, or
`CAP_SYS_NICE`, a strict read-only system view, and write access limited to the plan/status
directories remain mandatory compensating controls. Reintroduce any removed hardening directive
only after an installed-unit FUSE inventory canary proves that the child can still drop all UID/GID
credentials.

## Terminal-storage evidence conflict response

`status` or lifecycle health reporting `evidence_conflict` is an incident signal, not a cleanup
request. Stop the Dispatcher timer, allow any already-active oneshot service to finish, and create a
fresh verified Online Backup. Preserve the archive row, absence row, WorkItem identity, Runner
registry/archive/absence files, release receipt, and current/rollback links before investigating.

Compare only bounded identity fields: WorkItem ID, repository, Issue number, expected HEAD, archive
status, and permanent receipt digests. Do not edit SQLite, delete a Runner file, synthesize an
`ARCHIVED` response, repeat `PROVE_ABSENCE`, or use a prune command to make the projection healthy.
An absence without its bound unfinished archive, mismatched WorkItem/HEAD identity, or simultaneous
completed archive and absence receipt must remain blocked until the source of corruption is known.
If recovery requires replacing online state, use a validated backup and the full external
reconciliation workflow above; a binary or symlink rollback alone cannot resolve conflicting
durable evidence. Re-enable the timer only after `status`, lifecycle health, Runner read-back, and
an immediate ordinary sweep all agree, with the sweep producing no unexpected write.

## Control Host exact reclamation lifecycle

The Control planner hashes every candidate and writes one immutable, non-authorizing plan. It always
protects the current and immediate rollback release trees, the current release receipt, each of
those releases' newest successful DR root and source bundle, their DR inputs, and their uploaded
release archives. This preserves the rollback recovery chain when a newly activated current release
has not yet completed its own full DR drill. Permanent release, handoff, reclamation, and off-host
confirmation receipts are never candidates.

A DR bundle is not eligible until the protected off-host copy has been independently matched to its
on-host manifest and a permanent confirmation receipt has been written. The copy identifier is
metadata, not a network fetch; the operator must verify the off-host bytes first. Record that fact
exactly once with:

```bash
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-control-reclamation-plan-v1 \
  bundle-confirm \
  --bundle-id <exact-control-bundle-directory-name> \
  --manifest-sha256 <64-hex-on-host-and-off-host-manifest-sha256> \
  --off-host-copy-id <stable-storage-identity> \
  --apply
```

The command validates the complete on-host bundle and writes a root-owned mode-`0600` immutable
receipt keyed by the manifest digest. It does not copy or delete a bundle. A conflicting retry fails
closed.

```bash
sudo install -d -o root -g root -m 0700 \
  /var/lib/codex-dispatcher/control-reclamation-plans \
  /var/lib/codex-dispatcher/control-reclamation-receipts \
  /var/lib/codex-dispatcher/offhost-bundle-confirmations
sudo install -d -o root -g codex-dispatcher -m 0750 \
  /var/lib/codex-dispatcher/control-reclamation-status
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-control-reclamation-plan-v1 \
  plan --write-plan
```

The six-hour `codex-dispatcher-control-reclamation.timer` runs `auto-plan`. Fixed triggers are less
than 8 GiB available, more than four release trees, more than four DR roots, any verified bundle
target, at least 1 GiB reclaimable, or an on-host bundle missing off-host confirmation. It writes a
strict protected status and only writes an immutable plan when a trigger fires. Lifecycle health
routes unavailable, stale, and exact plan-ready state to the system Slack channel. This timer has no
apply flag and cannot delete assets.

Require `authorizes_apply=false`. Before a separately approved deletion, re-read the immutable plan,
rebuild the complete live inventory, and require the hashes and target set to match:

```bash
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-control-reclamation-plan-v1 \
  recheck --plan-sha256 <64-hex-plan-sha256>
```

Only after the operator has reviewed the exact paths and current byte estimate and separately
authorized that exact digest may apply run:

```bash
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-control-reclamation-plan-v1 \
  apply --plan-sha256 <64-hex-plan-sha256> --apply
```

Apply rejects inventory drift before deletion, then re-hashes each direct child immediately before
removing it. It writes a mode-`0600` permanent receipt before the first removal and after every exact
target. A partial failure is terminal evidence; do not rerun or edit it. Never translate a plan into
`rm` globs, `find -delete`, or broad filesystem cleanup.

## Runner release and image reference inventory

After every successful release sweep, backup, restore drill, health check, and timer activation,
run one post-receipt Runner planner service and re-run health, then record the operational boundary
without rewriting the transaction receipt:

```bash
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-release-handoff-v1 \
  --commit <40-hex-current-commit> --apply
sudo /opt/codex-dispatcher/current/scripts/codex-dispatcher-release-handoff-v1 \
  --commit <40-hex-current-commit> --status
```

The handoff command is idempotent and writes only its immutable root-owned receipt. It fails unless
the latest Dispatcher sweep, backup, restore drill, lifecycle health, Control timers, both exact
planners/statuses, and transactional reference ledger all postdate and agree with the release. It
never updates the release receipt and never authorizes reclamation. Exact current Runner or Control
plan-ready projections with durable system-channel receipts are the only permitted warnings;
stale, unavailable, capacity, lifecycle, or consistency alerts fail closed.

The isolated four-trigger canary exercises the real exact-plan, fixed-threshold, durable health
outbox, and system-channel routing code. Plan mode uses an isolated fake Slack publisher and makes
no network write:

```bash
sudo -u codex-dispatcher env \
  PYTHONPATH=/opt/codex-dispatcher/current/src \
  /opt/codex-python/current/bin/python3 -P -s -m \
  codex_dispatcher.reclamation_canary \
  --config /etc/codex-dispatcher/config.toml \
  --fixture-id rc_<32-lowercase-hex> --plan
```

A reviewed live projection requires the separate Slack fixture gate and `--apply`. It sends exactly
one alert root plus one threaded recovery to `system_channel_id`, writes zero Issue-channel
messages, uses an isolated SQLite outbox, and writes one permanent canary receipt. It never changes
the online Dispatcher database or Runner assets:

```bash
sudo systemd-run --wait --collect --pipe \
  --property=User=codex-dispatcher --property=Group=codex-dispatcher \
  --property=EnvironmentFile=/etc/codex-dispatcher/health.env \
  --setenv=CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1 \
  --setenv=PYTHONPATH=/opt/codex-dispatcher/current/src \
  /opt/codex-python/current/bin/python3 -P -s -m \
  codex_dispatcher.reclamation_canary \
  --config /etc/codex-dispatcher/config.toml \
  --fixture-id rc_<32-lowercase-hex> --apply
```

Install root-owned mode-`0600` files from the examples as:

```text
/srv/codex-runner/etc/image-provenance.json
/srv/codex-runner/etc/reclamation-rollback-references.json
```

This ledger is release-owned schema v2. It contains exactly the current release, its immediate
rollback release, and the currently configured image digest. Release activation atomically replaces
the ledger and records the exact before/after payload and SHA-256 under
`/srv/codex-runner/reclamation-reference-receipts/<commit>.apply.json`. Pre-sweep rollback restores
the prior payload and creates `<commit>.rollback.json`. A structurally valid but stale schema-v1
ledger is accepted only as the one-time input to this release migration; the reclamation planner
itself accepts only schema v2 and otherwise fails closed.

Create the immutable output roots once:

```bash
sudo install -d -o root -g root -m 0700 \
  /srv/codex-runner/reclamation-plans \
  /srv/codex-runner/reclamation-receipts
```

`image-provenance.json` is needed only for older final Runner images that predate the OCI revision
label. `publication_run` requires an exact successful publication run. `tree_equivalent` requires
recorded evidence that a local pre-publication build used the same Git tree. New images carry
`org.opencontainers.image.revision` and do not need a manual provenance row.

The rollback reference file must contain exactly the current release, the immediate rollback
release, and the configured image digest. Its `current_release_commit` must equal the live `current`
symlink or planning fails closed. Older permanent release/reference receipts remain audit evidence;
they do not keep assets protected beyond the explicit immediate rollback boundary.

Create the exact inventory and immutable plan:

```bash
sudo /srv/codex-runner/bin/codex-runner-maintenance-v1 \
  reclamation-plan --write-plan
```

The response and `inventory-<snapshot-sha256>.json` list every release commit/tree digest, final
Runner image ID/RepoDigest/source commit, current config digest, active WorkItem image digest,
permanent registry/archive/absence identity, rollback reference, exact deletion target, and Docker
reported unique-size estimate. Unlabelled base images and build cache are deliberately not inferred
as Runner releases and are never selected by this workflow.

The six-hour `codex-runner-reclamation-plan.timer` performs the same inventory read automatically.
It stores an exact plan only when one or more reviewed thresholds fire: less than 64 GiB host space,
more than four releases, any unreferenced Runner image, or at least 8 GiB expected reclamation. The
latest strict status is exposed to Control through the read-only `reclamation_status` Runner
operation; plan-ready, stale, and unavailable states are delivered through the system Slack alert
channel. This automatic path has no apply flag and cannot delete assets.

Continuous observation consists of the six-hour Runner planner plus the Control health timer. The
planner overwrites only strict `latest.json` when no threshold fires and writes an immutable exact
plan only after a trigger. Health reports plan-ready, stale, or unavailable status through the
system channel. Operators must not create dummy images or releases on the live Runner to force an
alert; use the isolated canary above. A real generated plan is only an alert and authorization
packet input. Before deletion, list its exact targets and current byte estimate again and obtain a
separate approval.

Planning never authorizes deletion. Immediately before a separately approved deletion, re-list the
exact targets and current estimate without writes:

```bash
sudo /srv/codex-runner/bin/codex-runner-maintenance-v1 \
  reclamation-recheck --plan-sha256 <64-hex-plan-sha256>
```

Only if `reinspection_matches=true`, `state_writes=0`, the target list is still approved, and no
WorkItem/registry/config/rollback reference changed may an operator run:

```bash
sudo /srv/codex-runner/bin/codex-runner-maintenance-v1 \
  reclamation-apply --plan-sha256 <64-hex-plan-sha256> --apply
```

Apply writes a mode-`0600` permanent `prepared` receipt before the first deletion and updates it
after every exact release tree or `docker image rm <image-id>`. It never invokes `docker image
prune`, `docker system prune`, wildcard deletion, or recursive shell deletion. If a partial failure
occurs, the receipt becomes `failed`; do not rerun or edit it. Deleted release trees are rebuilt
only from the recorded Git commit, and deleted images only from the recorded RepoDigest/publication
evidence. Protected current and rollback assets are never automatically recreated because they are
never eligible for the plan.
