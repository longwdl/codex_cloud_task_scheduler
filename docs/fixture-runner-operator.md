# Fixture Runner operator tool

`codex_dispatcher.fixture_runner_cli` is a standalone operator entry point for
the two authorized repositories, `longwdl/codex-dispatcher-fixture` and
`longwdl/codex-dispatcher-fixture-2`. It is not a Dispatcher operation or a Slack
control surface. It does not update GitHub, SQLite, model configuration, or
authentication, and it never restores or deletes dirty worktree files.

The September 22 two-host release
`1c9caad9335ce408ef00698135ca78c66541dd5a` includes this entry point. Fixture #58
exercised a real identity-bound stop, status/readback, duplicate-stop protection,
failed-generation pre-claim guard, and normal discard/archive cleanup. The earlier
September 20 standalone candidate supplied only read-only live coverage. See the
[acceptance record](live-test-evidence.md) for identities and receipt hashes.

Use this tool only from a reviewed source release on the Runner, as the
`codex-runner` service account. Root execution is rejected. Keep the protected
Runner configuration and pinned tool paths; do not substitute a writable config.
Use the existing clean environment pattern in `scripts/codex-runner-v1`, including
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, the release's `src` as
`PYTHONPATH`, and `/usr/bin/python3 -P -s`.

## Bind a negative test before applying it

Record the repository, Issue, WorkItem, Turn, generation ID and number, policy
digest, expected HEAD, and Runner host from trusted metadata. Verify the original
Issue's reviewed scope. The tool requires an established session and its actual
Runner receipts; successful login status alone is insufficient.

Set `CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS` to the exact authorized repository
in the service account's environment, then use `plan-stop` with the exact identity:

```text
python3 -P -s -m codex_dispatcher.fixture_runner_cli \
  --config /srv/codex-runner/etc/config.json plan-stop \
  --repository <authorized-fixture> --issue <number> \
  --work-item-id <wi-id> --turn-id <turn-id> \
  --session-generation-id <sg-id> --generation <number> \
  --policy-digest <sha256> --expected-head <git-sha>
```

Planning is read-only. Do not paste placeholder arguments or commands obtained
from an Issue into a shell. Construct arguments from verified operator metadata.

Before a stop, an administrator must provision the separate fixed directory
`/srv/codex-runner/fixture-operator-evidence` as a real, non-symlink directory owned
by `codex-runner`, mode 0700, beneath the existing protected parent chain. This is
operator evidence, outside all WorkItems. Do not place it inside a repository,
auth home, configuration directory, or container mount.

Run `stop` with the same identity, explicit `--apply`, and
`CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS` set to that exact repository in the
service account's environment. Preserve the JSON result without raw Docker
inspection output. The operation stops only the revalidated immutable container
ID, after durable intent. It does not stop SSH, Docker, the Runner host, or the
Control service.

## Read back; never blindly repeat

Use `status` with the same complete identity after a transport failure or stop.
Status does not require the fault-enable environment variable. A repeated `stop`
returns the existing completed receipt without sending another Docker stop.
An existing intent without a confirmed completion remains ambiguous. Container
absence alone cannot establish whether a timed-out stop ran. Do not delete the
intent, change identity arguments, or invoke Docker manually to bypass it.

A completed stop is only fault-injection evidence. Require the normal Runner and
Control to record the failed Turn and generation, and verify the expected
pre-claim guard without a new Turn, container, or `agent:dispatching` label.
Use the existing reviewed discard path for Issue/PR closure and archive. This
tool does not perform that cleanup or mark an archive complete.

## Diagnose an archive blocker

```text
python3 -P -s -m codex_dispatcher.fixture_runner_cli \
  --config /srv/codex-runner/etc/config.json diagnose \
  --repository <authorized-fixture> --issue <number> \
  --work-item-id <wi-id> --expected-head <git-sha>
```

The diagnostic command uses the existing Runner operation lock nonblockingly;
busy execution is a blocker, not permission to interrupt it. It reports bounded
metadata and paths, never file contents or a patch. Missing or contradictory
identity, changed HEAD, unknown filesystem state, and active execution must be
resolved before any manual recovery.

For terminal archive/absence receipts, `cleanup_verified` additionally requires
no matching container and no remaining WorkItem directory or image/staging
evidence. An `archiving` receipt remains incomplete. A diagnostic report is an
observation; it does not alter the durable lifecycle. Path lists are capped and
explicitly marked when truncated. Repositories with external Git filters,
tracked symlinks, or submodules remain blocked for manual inspection.

There is deliberately no `capture`, `clean`, `restore`, or forced archive
command. A tracked dirty path is not by itself proof that all its changes are
task-owned. Follow the [failed-session recovery runbook](runner-auth-and-session-recovery.md)
to preserve allowed task-owned bytes and a patch in a separate mode-protected
operator directory, verify their hashes, and obtain the required recovery
authorization before replacing any original bytes. Unknown or untracked changes
remain blocked. Normal identity-bound ARCHIVE_STATUS/ARCHIVE supplies the final
cleanup receipt.
