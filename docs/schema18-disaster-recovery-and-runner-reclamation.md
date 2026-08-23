# Schema-18 disaster recovery and exact Runner asset reclamation

This runbook has two independent workflows. The disaster-recovery drill may write only below a new
isolated recovery directory and may perform GitHub and Slack reads. Reclamation planning is
read-only except for immutable plan/inventory files. Actual reclamation is a separate destructive
operation and must never follow automatically from a plan.

## Measured recovery boundary

The receipt's `rto_milliseconds` measures one exact interval: selection of the newest validated
backup through isolated schema-18 restore, release-receipt validation, Control/Runner version
agreement, Runner registry/archive/absence reconciliation, GitHub Issue/PR/branch read-back, Slack
permalink read-back, and reconstruction of an empty Control application filesystem root. The
receipt records the backup age as the observed recovery point.

This is not a claim about VM procurement, OS installation, DNS, package mirrors, secret-manager
availability, or operator approval latency. Those infrastructure intervals are explicitly marked
unmeasured. The isolated empty root does not replace either live `current` symlink and never starts
a service. It contains the exact release, secret-free Control config, systemd units, and restored
database needed to prove the application reconstruction path.

## Schema-18 isolated drill

Preconditions:

- leave the current Control and Runner environments in place;
- require a committed schema-v2 release receipt for the exact current commit;
- require the newest retained backup to be mode `0600`, schema 18, integral, and free of foreign-key
  violations;
- require no unfinished Slack outbox row;
- place copied inputs in a `codex-dispatcher`-owned mode-`0700` directory and never print secrets.

On the Runner, collect one bounded tombstone snapshot as root. Replace the commit with the exact
value returned by `readlink /srv/codex-runner/current`:

```bash
sudo /srv/codex-runner/bin/codex-runner-maintenance-v1 \
  recovery-snapshot --current-release-commit <40-hex-current-commit> \
  > /var/tmp/runner-recovery-<40-hex-current-commit>.json
```

Copy that JSON to the Control Host, then prepare immutable inputs without changing the live state:

```bash
sudo install -d -o codex-dispatcher -g codex-dispatcher -m 0700 \
  /var/lib/codex-dispatcher/disaster-recovery-inputs \
  /var/lib/codex-dispatcher/disaster-recovery-drills
sudo install -o codex-dispatcher -g codex-dispatcher -m 0600 \
  /var/tmp/runner-recovery-<40-hex-current-commit>.json \
  /var/lib/codex-dispatcher/disaster-recovery-inputs/runner-<40-hex-current-commit>.json
sudo install -o codex-dispatcher -g codex-dispatcher -m 0600 \
  /opt/codex-dispatcher/release-receipts/<40-hex-current-commit>.json \
  /var/lib/codex-dispatcher/disaster-recovery-inputs/release-<40-hex-current-commit>.json
```

Run the isolated drill through a transient service so the existing protected environment file is
read without copying or displaying its tokens. `<UTC-run-id>` must name a directory that does not
already exist:

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
  --control-release /opt/codex-dispatcher/releases/<40-hex-current-commit> \
  --release-receipt /var/lib/codex-dispatcher/disaster-recovery-inputs/release-<40-hex-current-commit>.json \
  --runner-snapshot /var/lib/codex-dispatcher/disaster-recovery-inputs/runner-<40-hex-current-commit>.json \
  --execute-isolated --json
```

Acceptance requires `status=passed`, `database_schema_migrations=[1,...,18]`, identical Control and
Runner commits, all recorded external counts reconciled, `online_state_modified=false`, and a
mode-protected `receipt.json`. A failure writes `failed-receipt.json`; retain it and the source
backup, and remove only that exact recovery directory after investigation. Do not retry by reusing
the same recovery directory.

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
  <validated-schema18-backup> /var/lib/codex-dispatcher/state.db.recovered
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

## Runner release and image reference inventory

Install root-owned mode-`0600` files from the examples as:

```text
/srv/codex-runner/etc/image-provenance.json
/srv/codex-runner/etc/reclamation-rollback-references.json
```

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

The rollback reference file must contain the current release, every release named by the retained
rollback receipt, and every digest that a rollback config could restore. Its
`current_release_commit` must equal the live `current` symlink or planning fails closed.

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
