# Live test evidence

> The Codex Cloud-oriented sections are retained as historical evidence only. `exec:cloud` and the
> Cloud Environment are not part of the current SSH CLI target architecture.

## Repository admission, effective storage, and current-schema recovery — 2026-08-24

Commit `4aacd6ec85b506712b36fe57bfd10d7d902a7989` introduced the fail-closed
repository-class/runtime-profile matrix. The protected Fixture configuration names class `fixture`
plus `fixture-live-v1` and `fixture-exact-v1`; a synthetic `higher-value` evaluation remained hard
false in code. Both Control and Runner passed all 608 tests before the Runner-first handoff. The
release archive SHA-256 was
`cd3a38d7d80eced793db12622c0fb535ef0b0e7bc2efa2b0a1bb42d3b07f8d7a` and the protected
configuration SHA-256 was
`03a9b23b07ee9134e959c29523eca85a9a3a61f72d84b7cb67531f660efa084d`.

Commit `fc45a67a21d9e4ca0ec4d8a17202b286ea19f27d` added a read-only one-value terminal-storage
projection without rewriting the archive or absence ledgers. Its release archive SHA-256 was
`51af4a47bf3e1ca825a11130928064a8e0514f6cb04fc1f3b3c2e4303b1d3465`. All 612 tests passed
locally and under both real Linux service accounts. Live `status` and lifecycle health reported 23
WorkItems as exactly 21 `archived` plus two `absence_reconciled`; all other effective states,
including `evidence_conflict`, were zero. The two bounded overrides were Issues #24 and #26, whose
immutable raw `prepared` archive rows are superseded by exact permanent Runner absence receipts.
The manual handoff and immediately timer-triggered sweeps were both `idle`, each with four GitHub
reads, zero writes, and zero failures. Final health had zero alerts and approximately 74.39 GB
available on the Runner.

The current-schema isolated recovery receipt is
`/var/lib/codex-dispatcher/disaster-recovery-drills/20260823T235517Z-fc45a67/receipt.json`. It used
backup `state-20260823T235539.753045Z.db`, age 41 seconds, and measured an application RTO of exactly
100,789 milliseconds. Migrations 1 through 19, 23 WorkItems, 21 Runner archives, two Runner
absences, 23 GitHub Issues, 19 Pull Requests, and 39 Slack receipts all reconciled. Control and
Runner agreed on `fc45a67`, the empty-host rebuild manifest SHA-256 was
`7d70e61797be17da5416c26407a0103f371f51f3837fdfcd8104a1b95d6546cc`, and
`online_state_modified=false`. After the Dispatcher timer was restored, the observed sweep was
again idle with four reads and zero writes.

The refreshed non-destructive Runner reclamation plan is
`/srv/codex-runner/reclamation-plans/d16d4984f3b63e75101588310518d32639b19f3151bc8255a06f494aa3f4c6d8.json`.
Inventory SHA-256 `da56611aad7060cc3d2c0e9bf5523dbcd5b6233321d2677dbe32b4459770c911`
protects current `fc45a67`, rollback `4aacd6e`, and the configured image digest. It lists 11 exact
old release trees, zero images, and an estimated 42,512,384 bytes. The plan explicitly reports
`authorizes_apply=false`; no deletion was attempted or authorized.

## Live checkpoint, resume, and fresh-Audit Fixture — 2026-08-24

The canary series used the dedicated private Fixture only. Runtime releases `28b19ca` and
`97bfc8a` corrected the Structured Outputs subset and aligned the read-only Audit field contract;
neither release weakened the Runner's read-only repository mount or trusted post-process Git
verification.

Issue `#41` first proved that an unsupported provider schema is a terminal, observable failure
rather than an ambiguous retry. Its exact 8,589,934,592-byte WorkItem image was later archived by
the ordinary lifecycle receipt. Issue `#42` then proved the next independent boundary: a fresh
Audit returned `changed_paths=["README.md"]` while trusted Git remained at the input HEAD. Runner
rejected the Turn as `audit_mutation_forbidden`; its exact 8,589,934,592-byte image was archived,
Issue `#42` remained open with `agent:discard`, and Draft PR `#43` was closed without merging.

Issue [`#44`](https://github.com/longwdl/codex-dispatcher-fixture/issues/44) completed the positive
path. WorkItem `wi_5b34425042411a953a08f945` used implementation session
`01a02fdc-a5f9-7c61-a475-8998cf4495c6` to publish checkpoint
`e1ff17a34d5f4f9ed2cb6cf0658778fc843836a6`, then resumed that same session and published final
HEAD `b0f3201f0ccc6d13cca179524a660fa58f109be3`. Both completion-gate evaluations passed on the
final HEAD. The independent Audit used fresh session `01a02fe0-a06a-79f1-a9e8-73533b1516f4`,
returned `changed_paths=[]`, left trusted Git unchanged, and produced a delivered Slack Turn
receipt. GitHub Actions run
[`32658051742`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32658051742)
completed successfully for that exact HEAD.

After explicit operator authorization, Draft PR
[`#45`](https://github.com/longwdl/codex-dispatcher-fixture/pull/45) was marked ready and merged as
merge commit `02d9eaff6605203be063c5e06c88937fcd191477`. The next Dispatcher sweep changed the durable
WorkItem from `review` to `completed`, retired its Audit generation, and projected
`agent:completed`; two following sweeps were idle. Issue `#44` remains open as the durable audit
entry point. At that checkpoint its task branch was subject to the configured 30-day retention and
its exact 8,589,934,592-byte image was subject to the configured seven-day completed retention;
neither had been manually removed.

Two later configuration-driven canaries replaced that pending state without bypassing lifecycle.
First, a transactional release temporarily set `completed_retention_seconds=1`. One ordinary sweep
archived only WorkItem `wi_5b34425042411a953a08f945`; schema-12 recorded expected HEAD
`b0f3201f0ccc6d13cca179524a660fa58f109be3`, Runner response SHA-256
`fcfd7578b87a83c8dcc7eb27c27c49a0fa649a818554f789d0a791794313eca2`, and
`reclaimed_bytes=8589934592`. The image became absent while its registry and permanent archive
tombstone remained. Available space increased from 65,817,686,016 to 74,407,575,552 bytes. The
normal seven-day value was then restored with the exact pre-canary configuration SHA-256
`dab6e3c1acb488521b30b009049f894b208d8f8cb451177b70a6995c2ade9edb`.

Second, release `5a1e577408964d6cf53db08854927c51951a6bc9` added the inclusive
`terminal_branch_retention_cutover_at` rollout boundary. The live candidate used one-second branch
retention and cutover `2026-08-23T19:29:16.047801Z`, exactly the #44 terminal Runner-evidence time.
One ordinary sweep performed 12 GitHub reads and one write, deleted only
`codex/issue-44-5b3442504241`, and committed a completed cleanup receipt with outcome `deleted`,
request SHA-256 `ebf36800e1f30f39c1945a8a0edcac149ebc385e69943a1f4922b707c9592256`, and the same exact HEAD.
The 16 older present task branches and five older absent branch identities received no new cleanup
record. A GitHub read then returned 404 for #44's ref. Release
`a2f66949f4847d731b7c2ea1f23401884dc31d5c` restored the normal 30-day value and removed the
temporary cutover; the handoff sweep was idle with four reads and zero writes. Both canary and
restore releases passed all 604 tests on Control and Runner. Final health had zero alerts, no
pending or blocked cleanup, all Control/Runner timers active, and 74,395,394,048 bytes available.
No Docker prune, manual WorkItem deletion, direct Git ref deletion, or SQLite edit was used.

## Schema-18 full disaster-recovery drill and exact reclamation plan — 2026-08-23

The final two-host runtime commit is `6da473495333bdba16e8f4272c00741b5f9c6130`, with release
archive SHA-256 `69cf08ac72307aac76851edfc8b89b0a8315739780696b425afd630599e3de63`.
Both the Control and Runner service accounts passed all 593 tests before the Runner-first switch.
The committed receipt preserved `aa2fff96511e81d499531208ec10dba088a009bd` as the exact prior
Control and Runner release and left all Control timers stopped for manual handoff.

The successful isolated drill receipt is
`/var/lib/codex-dispatcher/disaster-recovery-drills/20260823T150830Z-6da4734/receipt.json`.
It selected backup `state-20260823T150416.930131Z.db`, whose SHA-256 and the restored database
SHA-256 were both `96c13580722c624296ff75af82d60d63a239021548872111a0b0b2d47a6d8aaf`.
The measured application recovery interval was exactly 80,719 milliseconds and the backup was 266
seconds old at drill start. This interval covered backup selection, isolated restore, schema and
release validation, an empty Control application-filesystem reconstruction, and Runner, GitHub,
and Slack read-back. VM/OS/network/credential provisioning remained explicitly unmeasured.

The restored database passed integrity, foreign-key, and exact migration-1-through-18 checks. It
contained 19 WorkItems. Runner reconciliation proved 17 permanent archive tombstones and two
absence tombstones; GitHub reconciliation proved 19 Issues and 17 PRs; Slack GET-only read-back
proved all 31 durable receipts. The rebuilt-root manifest SHA-256 was
`deaf36453e7cbfe24f90d05af14b4fdaf252f06b94ac661a755dede3c66eb90e` and the receipt recorded
`online_state_modified=false`. The failure boundary never replaces the online database, switches a
live symlink, or changes a Runner Turn; a failed attempt retains its backup and removes only its
exact isolated recovery root after inspection.

Runner reclamation plan `06b8114af8309de53ac79b587accc8ae3ac41cd8809685be1e59c8f5cf7f701e`
is permanently stored below `/srv/codex-runner/reclamation-plans/` with reference-inventory
SHA-256 `3c3cc1c784884631689a995d0ff7a8abe60efe95a4f38ea4ed4996bfb5f38e03`.
It protects current release `6da4734`, rollback release `aa2fff9`, and configured/rollback image
`ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:da3662343e86ebeeba97f54f1c7f03faf03b988e07677cbd171a80d9903c772d`.
The inventory had 21 release trees, two addressable final Runner images, no active WorkItem image
binding, 17 archived registry identities, two absence identities, and no blocked reason. Twenty-two
unaddressable Docker build intermediates inherited Runner labels but had neither RepoDigest nor
RepoTag; they were deliberately excluded rather than assigned guessed provenance or deletion
identities.

The exact release targets are `081a4f3`, `134cafe`, `257fa86`, `2ec4dc9`, `2f7747f`, `3b7858b`,
`8654269`, `9d88507`, `a630b99`, `a8800c4`, `bce452a`, `c2e27b0`, `cc200bf`, `cc9ccab`, `e7bde53`,
`ed50a1e`, `f38959a`, `f98a512`, and `fd2f43b`; their full commits, paths, allocated bytes, and tree
SHA-256 values are bound in the permanent plan. Their total is 66,887,680 bytes. The one exact image
target is image ID `sha256:a36f9077ec5e918a58152f7e99ec31a0f1ae73d9a6a85602d47d312324ee4978`,
RepoDigest `codex-cloud-task-scheduler-runner@sha256:a36f9077ec5e918a58152f7e99ec31a0f1ae73d9a6a85602d47d312324ee4978`,
source commit `5c268fb6fe4bb59ebab0cc9f84570bfff47de90b`, provenance `tree_equivalent`, zero
containers, and Docker unique-size estimate 351,700,000 bytes. The combined estimate is 418,587,680
bytes. Two separate rechecks, including the final pre-approval check, reported
`reinspection_matches=true`, `state_writes=0`, and `requires_separate_apply=true`. After explicit
operator approval, a third identical recheck preceded the separate apply. Permanent receipt
`/srv/codex-runner/reclamation-receipts/06b8114af8309de53ac79b587accc8ae3ac41cd8809685be1e59c8f5cf7f701e.json`
is root-owned mode `0600`, has SHA-256
`c437e1dcc83ccaf7c947b73abbc0bf7407e12762fdd7525a92ad0b6023946a3c`, and records
`status=reclaimed`, all 19 release commits, the one image ID, and `reclaimed_bytes=418587680`.
Post-checks found only current `6da4734` and rollback `aa2fff9` release directories, proved the old
image absent and the configured image present, and observed available space increase by 418,828,288
bytes. No Docker prune command was executed.

The post-release preflight and both observed sweeps were `idle`, with four GitHub reads, zero
writes, and zero failures. Fresh Online Backup `state-20260823T151231.030355Z.db` was 614,400 bytes
with integrity `ok`; its restore drill passed migrations 1 through 18, zero foreign-key violations,
and temporary-file cleanup. Final health reported zero alerts, zero active or blocked WorkItems,
zero pending or blocked archive/branch cleanup, Runner capacity admissible with 74,450,329,600 bytes
available, all four Control timers enabled/active, and the Runner capacity timer enabled/active.

## Safe inactive-Turn abandonment and schema-18 release — 2026-08-23

Commit `cc9ccab4f6679947b3d15957924bd0f25a95ee14`, archive SHA-256
`fdeebc2e0fb95f2c856c2bb2c179b4f85b8c882260ea532176a3622ae7e1b331`, adds the protocol-v2
inactive-Turn abandonment boundary. Both real Linux service accounts passed all 575 tests,
compilation, shell checks, and unit verification before the Runner-first two-host handoff. The
read-only release plan authorized no apply, performed zero state writes, and found no active
service. The committed schema-v2 receipt retained the exact prior `c2e27b0` Control and Runner
links, switched both links to `cc9ccab`, and stopped at `handoff_required` without starting a timer.

Runner `STOP` is now abandonment-only: it is accepted only under the global Turn lock, only for
protocol v2, and only after an exact final Docker inspection proves the bound generation container
`stopped` or `absent`. Running, unavailable, malformed, legacy, or identity-drifted observations
fail closed. The operation never calls Docker stop, kill, or remove. Control exposes a read-only
plan and a separately double-gated apply; the schema-18 receipt, failed generation, blocked Turn,
and blocked WorkItem commit atomically and replay idempotently. A lost STOP reply is recovered from
the durable STATUS receipt without restarting Codex. Target-host tests exercised stopped/absent,
observation failure, global-lock conflict, process-loss simulation, lost-response recovery, and
repeated apply. No live Codex process was destroyed to manufacture acceptance evidence.

The first post-release sweep migrated SQLite through migration 18 and returned `idle` with four
GitHub reads, zero writes, zero failures, and 3,534 milliseconds elapsed. Direct and systemd health
reported zero alerts, zero active or abandoned Turns, both Runner capacity admissions true, and
approximately 74.1 GB available. The pre-activation backup and restore drill passed integrity,
foreign-key, migration-1-through-18, and temporary-file cleanup checks. All four Control timers and
the system-level Runner capacity timer were restored after the observed handoff.

## Final Runner restart, remount, egress, and auth-status acceptance — 2026-08-23

The final `cc9ccab` runtime was tested with the write Dispatcher and health timers stopped, zero
active Turns, an acquirable Runner global lock, zero Docker containers, and Control backup
`state-20260823T093213.279540Z.db` verified `integrity=ok`. All 19 retained WorkItems were already
terminal and reclaimed, so no real WorkItem mount or auth copy was mutated for this acceptance.

Restarting the rootless Docker user service preserved the exact `codex-egress` bridge and returned
with zero containers. Credential-free host probes allowed `api.openai.com` only through the audited
loopback proxy and denied direct public TCP/443, private, metadata, non-proxy loopback, a disallowed
public hostname, and another local UID. The disallowed CONNECT added exactly one metadata-only
audit record. Credential-free containers repeated proxy-only allow plus direct public, private,
metadata, gateway, disallowed-proxy, and second-container denial. Stopping Squid denied both host
and container proxy paths; after service restart, one immediate request encountered the readiness
window, then listener read-back and bounded retry succeeded for both paths. Every probe container
was removed and Docker returned to zero containers.

An isolated temporary WorkItem-shaped Codex home ran the deployed fixed Docker login-status plan.
Before and after the Runner host reboot it returned the exact ChatGPT-login success condition; the
Runner-wide seed remained byte-identical, the status command did not change the isolated auth copy,
and each temporary home was removed. This proves the deployed per-WorkItem login-status gate and
writable isolation; it does not prove that the status command performs a provider request. No token
was expired, edited, printed, or logged. Current policy therefore relies on the same exact
login-status check immediately before every START/RESUME, fail-closed handling of the real command,
and explicit operator re-login recovery. Codex-managed token refresh is not a separate Dispatcher
admission gate.

The non-production Runner host then rebooted after a second zero-container and clean-unmount
preflight. SSH disconnect and reconnect were both observed. Firewall, proxy, and rootless Docker
started automatically; the rootless daemon again had zero containers. A dense 64 MiB ext4 fixture
survived the reboot, remounted as `fuse.ext4` with `rw,nosuid,nodev,user_id=1002,group_id=1002`, and
retained its marker. Clean unmount and read-only `e2fsck` passed before the exact fixture image and
mount directory were deleted.

After all five timers were restored, two ordinary sweeps were `idle`, each with four GitHub reads
and zero writes. The fixed Runner capacity reply was protocol v2 with both admissions true and
approximately 74.06 GB available. The intentionally stopped timers first produced the expected
two-alert Slack episode; the final health run reported zero alerts and projected its recovery. The
final Online Backup `state-20260823T095044.203108Z.db` is 585,728 bytes with integrity `ok`; its
restore drill verified zero foreign-key violations, migrations 1 through 18, and exact temporary
restore cleanup.

## Durable GitHub API metrics and cursor-age health — 2026-08-23

Commit `c2e27b0a5b81b523c6ad4d72964505aa6605ff06`, archive SHA-256
`e78181ffe1a8f6fe6eb18496a20cddabb6056acf37398ef4f1c39fea0d0ce47e`, passed 569 tests under
each real service account, compilation, shell checks, and unit verification before the two-host v2
release handoff. The plan reported zero writes and no active service; the committed receipt retained
the exact `cc200bf` prior links and left all Control timers stopped.

The first observed sweep migrated SQLite additively through schema 17 and returned `idle`. Its
process result and first durable `github_api_sweep_metrics` row agreed exactly: sequence 1, outcome
`success`, status `idle`, four reads, zero writes, zero failures, 3,280 milliseconds, no rate-limit
error, Core 5,000/5,000, and GraphQL 4,970/5,000. The daily terminal-audit cursor remained the prior
successful value because its configured interval was not due.

The fresh Online Backup was 573,440 bytes and the restore drill verified integrity `ok`, zero
foreign-key violations, migrations 1 through 17, and removal of the temporary restore. Lifecycle
health read the durable row rather than journal output: one metric, latest age 31 seconds, terminal
cursor age 9,506 seconds, zero alerts, both Runner admissions true, and approximately 74.1 GB Runner
availability. All four Control timers and the Runner capacity timer were active after handoff.
Rollback to schema-16 code requires the preserved pre-migration backup, not only a symlink change.

## Durable release-v2 plan, receipt, and rollback canary — 2026-08-23

Commit `a630b9976350cab01bd2cbe911b2e2dbff6917f5` introduced the stable-name release tool's
schema-v2 transaction behavior. Its candidate tool first returned a read-only plan with
`authorizes_apply=false`, `state_writes=0`, no active service, and the exact prior `9d88507` links.
The installed v1 tool then bootstrapped that commit: both real service accounts passed 565 tests,
compilation, shell syntax, and unit verification before the Runner-first switch. The first manual
sweep was `idle` with four GitHub reads and zero writes; backup, schema-1-through-16 restore drill,
health, and timer reactivation all succeeded.

The deployed v2 tool next planned and applied commit
`8654269d19b9e7e8f597dcd876f30ab1711f7311`. Its root-only receipt directory and file were mode
`0700` and `0600`; status proved both candidates created and activated, both prior links, the exact
archive digest, and `committed/handoff_required`, while all four Control timers remained stopped.
An immediate automatic rollback was correctly refused because the recorded Dispatcher
`InvocationID` had changed. Bounded journal evidence showed why: an already-scheduled `idle` sweep
started two seconds after apply began and finished with four reads and zero writes before quiescence.
The tool had sampled InvocationID before stopping timers, creating a safe false-positive drift.

Commit `257fa8694e2c9440d3494444eedc7380da50b6b3` moved the rollback identity sample after bounded
natural service quiescence and both nonblocking host-lock probes. Local and both-host validation
again passed all 565 tests. With timers already stopped, its committed receipt matched the live
InvocationID exactly; `--rollback --commit ... --apply` restored both `8654269` links and units and
atomically changed the retained receipt to `rolled_back/rollback_observed` with operation
`rollback`. No candidate was deleted and no timer or sweep was started by rollback.

The final immutable runtime release on both Control and Runner is
`cc200bf5ba7ef6b0a03b371c381b4216abd6865e`, archive SHA-256
`e98684792878d1226605d49adfe82b02f7bcf60e80c56b0b7fef7db1cfc433db`. Its read-only plan and
two-host 565-test apply succeeded; the schema-v2 receipt is committed with exact links and matching
InvocationID. The observed handoff sweep was `idle` with four reads and zero writes. A fresh Online
Backup and restore drill reported integrity `ok`, zero foreign-key violations, and migrations 1
through 16. Final health reported zero alerts, no active turn or pending/blocked archive or branch
cleanup, both Runner admissions true, approximately 74.1 GB available, and all four Control timers
plus the Runner capacity timer active. The first sweep has closed automatic rollback for this final
release; database or external-state rollback must use the preserved backup and recovery-first
procedure.

## Transactional release, sweep budget, terminal retention, and capacity canary — 2026-08-23

Commit `bce452a0a7be79de0fbf6e03da0dea0268b6af12` is deployed on both Control and Runner. Its exact
archive passed 560 tests under each real service account, compilation and wrapper syntax checks,
and systemd unit verification before the Runner-first/Control-second atomic switch. The
transaction created a fresh Online Backup, preserved the previous two-host release links, left all
four Control timers stopped, and required a manually observed sweep before reactivation. The only
unit diagnostics came from unrelated pre-existing `snapd` and `cloudmonitor` units.

The first schema-16 sweep completed the daily terminal GitHub audit with 77 read-only commands,
zero writes, and 55,366 milliseconds elapsed. It persisted the `terminal_github_audit` cursor; an
ordinary follow-up used four read-only commands and approximately three to four seconds. Terminal
Issues remain open. The configured branch retention is 2,592,000 seconds, and no branch was old
enough for ordinary deletion.

Commit `9d8850799bd143f810dc79b60e7280af931b7d0f` added a two-stage, exact-target Fixture canary without
changing that global retention. Both hosts passed 562 tests before activation. The first stage bound
Issue #38, WorkItem `wi_b1badef21a8c1d5b8c029c7c`, branch
`codex/issue-38-b1badef21a8c`, and expected HEAD
`56f0fc2f7a0a5030e32e9fb1c2f8613b91a41809`; it required ordinary recovery to be idle, persisted one
PREPARED request with SHA-256 `48e06a578f0fd4857d05567c9db3ec033cec30f6ce21219ae30634d10c5075d6`,
deleted only that exact ref, independently proved absence, and discarded the local receipt. Issue
#38 remained open and completed, while PR #39 remained merged at the same head. The second guarded
stage rejected any DELETE operation, observed the absent ref, and completed the same record as
`reconciled_absent`. SQLite contains exactly one prepared and one completed cleanup event. Both
mode-`0600` online backups passed integrity checks. Two subsequent ordinary sweeps returned `idle`
with four GitHub reads and zero writes each. Final health reported one cleaned branch, zero pending
or blocked cleanups, zero alerts, all four Control timers active, both Runner capacity admissions
true, and approximately 74.1 GB available.

The Runner capacity canary stopped the Dispatcher timer and raised only the protected
`work_item_disk.host_reserve_bytes` above observed availability. Runner reported both Turn and
provision admission false, and Control opened one deduplicated Slack episode containing
`systemd_timer_not_active`, `runner_turn_capacity_low`, and `runner_provision_capacity_low`.
Restoring the exact Runner configuration and timer produced one threaded recovery; a third healthy
check emitted no duplicate. Final read-back found schema migrations 1 through 16, integrity `ok`,
zero foreign-key violations, all five timers active, approximately 70 GiB Runner space available,
and both capacity admissions true.

## Slack health alerts, bounded backup retention, and restore drill — 2026-08-23

Commit `88e25f2514f59ee0f68377d1045a55a2e00e75e5` added schema 14, the durable health-alert
outbox, Slack alert/recovery projection, validated backup rotation, and the protected restore drill.
The final exact archive passed 543 unit tests in 89.867 seconds as the real Control service account
under a short service-owned temporary root with unit-equivalent `umask 077`; compilation, wrapper
syntax, diff checks, and `systemd-analyze verify` also passed. The only systemd diagnostic was an
unrelated pre-existing `snapd.service` warning. Runner code and its active release were unchanged.

Before migration, the stopped-and-idle Control service created Online Backup
`state-20260823T042620.907876Z.db`. Control then atomically switched from release
`f38959a820be99296e723d8c8dd6202c9d690d02` to the exact commit above. The first dispatcher run
migrated versions 1 through 14 and returned `idle`; the live database passed `integrity_check`, had
zero foreign-key violations, and retained zero active health alerts after the canary.

The first new backup service created mode-protected
`state-20260823T042930.026736Z.db`, validated the complete canonical set before deletion, removed
two excess valid copies totaling 991,232 bytes, and retained eight databases: the original
schema-13 anchor `state-20260822T185953.695987Z.db` plus the newest seven. The credential-free,
network-isolated restore service then restored the newest backup to a temporary database, verified
the exact shipped migration ledger 1 through 14, `integrity=ok`, and zero foreign-key violations,
and removed the temporary restore.

The Slack delivery canary first confirmed that a healthy report emitted no message. Control then
stopped only the still-enabled restore-drill timer. Health failed with exactly one
`systemd_timer_not_active` alert and durably delivered one
[`alert` root message](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787459838171809).
After restarting the timer, the next health run delivered one
[`recovery` reply](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787459863402639?thread_ts=1787459838.171809&cid=C0BR2D0MS8Y)
in that root thread. A third healthy run emitted no duplicate. SQLite contains exactly those two
immutable delivered outbox rows and no active alert.

Final read-back found dispatcher, health, backup, and restore-drill timers all `enabled/active`, and
the most recent result for each corresponding service was `success`. The service-owned candidate
test directory was deleted after validation, reclaiming 2,768,823 bytes. Rollback is the retained
`f38959a` release plus the pre-migration Online Backup; because schema 14 is additive, rollback must
restore that backup before switching binaries if schema-level rollback is required.

## Trusted absence, Sol routing, retention, and monitoring — 2026-08-23

The owner authorized the remaining historical cleanup and all Fixture-only review/merge actions.
Issue #1 was migrated from its stale `exec:cloud` label to `exec:ssh-cli` after the trusted
`agent:discard` event `29861482832`. WorkItem `wi_3a97e3d99c30bdcbb50501dd` was durably classified
`abandoned` at HEAD `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, archived, and reclaimed 131,688 bytes. The six
superseded Draft PRs #3, #5, #7, #9, #11, and #17 were then closed without deleting or rewriting
their remote branches; their original head SHAs remain available for audit and rollback.

Commit `4e27117f654af027ea5884fbb4c1c1d933f0b4ec` added protocol-v2 trusted Runner absence receipts and
schema-13 Control evidence. `PROVE_ABSENCE` runs under the Runner global lock, requires registry,
workspace, archive/absence staging, image, and mount state all to be absent, writes a permanent
mode-`0600` receipt, and blocks later recreation. Issue #24 / WorkItem
`wi_59089b353ecda298b262a063` is bound to expected HEAD
`acb03e63f045ec5642d9e19fe44b659b53834284` and evidence SHA-256
`7e6971d83f3bf4318f631ea11d7b93b8727bc5bf9dfc14800b7b80e0ae32ac83`. Issue #26 / WorkItem
`wi_6bee727d623ec61a2d31cf11` is bound to HEAD
`2d7a71747d2ca11291fae8f4dd64145b8818948d` and evidence SHA-256
`4ef0efd7235f1245d15f840e64efd276d56e6a9d66d125e0e495746c074e2025`. Control and Runner hashes
matched exactly; the deliberately `prepared` archive rows plus absence rows are terminal and cannot
be selected again.

The autonomous routing canary used Fixture Issue
[`#38`](https://github.com/longwdl/codex-dispatcher-fixture/issues/38), WorkItem
`wi_b1badef21a8c1d5b8c029c7c`, and branch `codex/issue-38-b1badef21a8c`. The Issue specified neither
an agent nor a model. The policy-pinned root session `01a02c79-2d93-7351-9463-45244d672e60` ran
`gpt-5.6-sol` at `xhigh` and autonomously delegated two independent read-only checks to
`spark_worker` / `gpt-5.3-codex-spark` at `medium`: child sessions
`01a02c79-a396-73e0-ab4a-c202e3bc85f8` and `01a02c79-b61e-7de3-922d-a3eec90650ad` used 21,999 and
38,449 tokens. The canonical delegation receipt SHA-256 is
`ab7f87077813cd02faa6e2b30b2e08fcd4abf7efff1e48559c8ba5691bbae3f6`. The Implementation Turn
published HEAD `56f0fc2f7a0a5030e32e9fb1c2f8613b91a41809`; mandatory fresh Audit generation 2 used distinct
session `01a02c90-0d10-74d2-af57-0e57cc8cf7ac`, started and finished at that same head, and has
delegation receipt SHA-256 `4623f7a6a8abe62d7ed134563475aa296532dc8274bbf719bf2ac09ee9dc74fa`.

The first complete canary exposed three fail-closed integration gaps before any merge. Commit
`cbfe210f079f78dfe4a731d2327b500772b2b6e7` creates or recovers the single Draft PR before the
completion gate so a pull-request-triggered Actions workflow can exist. Commit
`fd2f43bdab8ae8d4c6a7fee2c53eda19228a86e8` permits only fresh Audit planning/handoff to validate
the intentionally still-running Issue state. Commit `f98a5128466991b140223866c140eda494948766`
routes an operator-reactivated, already-passed implementation gate directly to its pending fresh
Audit instead of replaying Implementation. No failed attempt started a second Implementation Turn.

Draft PR [`#39`](https://github.com/longwdl/codex-dispatcher-fixture/pull/39) changed exactly one
README marker line. Actions run
[`32613681474`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32613681474) completed
successfully at the exact head above. After independent read-back it was marked Ready and merged by
the explicitly authorized operator action; merge commit was
`61923129f4321680f602116c621539851fe6edbb`. The next recovery-first sweep projected Issue #38 to
`agent:completed` without another Turn.

With a fresh protected SQLite Online Backup and no active Turn, Control temporarily set
`completed_retention_seconds=1`. The only eligible completed WorkItem was #38. One normal sweep
returned `archived`; Control receipt SHA-256 is
`ed39a4d9edc788d55818976ae46258a871fa968ea8e35a623a87a971a62a039a`, Runner archived at
`2026-08-23T03:09:03.814453+00:00`, and exactly 8,589,934,592 bytes were reclaimed. The mode-`0600`
v2 `bounded_image` tombstone and permanent registry remain; image, workspace, mount, staging, and
container state are absent. Available Runner space rose from 65,456,357,376 to 74,046,369,792 bytes
and filesystem use fell from 19% to 9%. Live retention was then atomically set to the reviewed
normal value `604800` seconds (seven days), configuration SHA-256
`7b928c853bf6fd81c8d5473168715a5874bd1cef172c67a055873f57fcb93694`.

Commit `f38959a820be99296e723d8c8dd6202c9d690d02` added credential-free, network-isolated, read-only
15-minute monitoring. Control checks SQLite integrity/foreign keys, active-Turn age, 24-hour blocked
WorkItems, 15-minute archive pending, ambiguous/blocked archives, overdue disposition/retention,
and a fixed systemd unit allowlist. Runner exits nonzero unless its reserve and one additional
8-GiB image remain admissible. Its exact Git archive SHA-256 was
`f728796f6fb65e42eecf47921e588b58edf690d979f72d9c5d35cc8cafbe2870`. All 534 local tests,
`compileall`, wrapper syntax, and diff checks passed. Exact archive copies passed the same 534 tests,
compilation, and wrapper checks under both real service accounts after using their protected,
service-owned validation roots and the unit-equivalent `umask 077`; earlier attempts failed only
because administrator cwd/TMPDIR ownership or `umask 0002` intentionally violated existing test
preconditions.

Runner switched first, then Control. The first real Runner capacity service reported
74,033,803,264 available bytes, both admissions true, and zero shortfall. The first Control health
service reported 19 WorkItems, 17 archived records, two absence reconciliations, zero active Turns,
zero blocked/pending/ambiguous archives, zero alerts, `integrity=ok`, zero foreign-key violations,
and all dispatcher, backup, and health timers loaded/enabled/active. The concurrently activating
normal dispatcher oneshot was permitted and later returned strict `idle`. Both hosts run the same
runtime release `f38959a`; systemd verification emitted only unrelated pre-existing vendor-unit
warnings.

Final hygiene removed 20 old Control releases and 29 old Runner releases, leaving the current
release plus `f98a512` and `fd2f43b` rollback anchors on each host. The initial backup cleanup
removed 161 old SQLite backup/sidecar files (14,512,128 bytes; deletion-manifest SHA-256
`d8ce4a3bd7feca32c6e72c38e34ce65d60772ec1dc0d7e7c60004b3391dcd18e`) only after validating all
retained databases. A final successful Online Backup then rotated out the oldest post-migration
copy and removed the two transient read-only-validation sidecars. Eight mode-`0600` backups remain:
the schema-13 pre-migration boundary `state-20260822T185953.695987Z.db` and the seven newest complete
backups through `state-20260823T034617.871533Z.db`. Release directories are recoverable from Git;
deleted database copies and the reclaimed #38 image are not, so rollback uses one of the retained
validated backups and never only an old binary symlink.

## Completed WorkItem live reclamation canary — 2026-08-23

The first real reclamation canary used completed Fixture Issue
[`#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12), merged PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13), and WorkItem
`wi_594a1305a087ff78a0ab32f8`. Control already held one finished Turn, the exact task branch
`codex/issue-12-594a1305a087`, and published HEAD
`41e67598b506dcbfeac00e5871a812e6e9874078`. Runner read-back found the original registry and
pre-bounded workspace intact: registry and `workspace.json` matched, the branch and HEAD were exact,
and the worktree was clean. GitHub read-back independently found the Issue `agent:completed` and PR
#13 merged from the exact task branch and published HEAD.

The WorkItem predated bounded ext4 disks, so it had no image. The host had 22,545,264,640 available
bytes, below the configured 8-GiB-image-plus-16-GiB-reserve admission boundary; the reserve was not
lowered. With the Dispatcher timer stopped, no active Turn or container, and the Runner global lock
held, the migration temporarily loaned the allocated image of already-blocked Fixture Issue #20.
Both source trees were first copied to mode-protected Runner-local backups and compared. The #12
tree was then installed into a newly formatted, dense, mode-`0600`, 8-GiB ext4 image using the same
root ownership, mount, fsck, and identity constraints as normal provisioning. Stable content SHA-256
was `9b68cafd42e0f08df35e52b7a6b8f420a8fb8ce8ce0f37a014ac23b5d9cd6808`; the comparison excluded
only ext4 `lost+found`, Git's mutable `.git/index`, and fuse2fs `.fuse_hidden*` unlink artifacts.

The migration failed closed twice before acceptance. The first attempt stopped after format/mount
and before copying because a raw filesystem root lacked the owner/mode adjustment performed by
normal `provision()`. The recovery applied that same adjustment and copied from the verified backup.
A later byte-tree comparison rejected the expected Git index stat-cache update and one fuse2fs
hidden unlink file; path-level comparison found those were the only differences, after which the
stable-tree boundary above, exact branch/HEAD, clean worktree, and metadata identity all passed.
Neither stop invoked `ARCHIVE` or changed Control state.

Pre-canary Online Backup `state-20260822T171800.915615Z.db` was mode `0600`, passed
`integrity_check`, was schema 12, and contained zero active Turns and zero archive rows. Control
temporarily set `completed_retention_seconds=1`, reloaded the protected configuration successfully,
and ran exactly one normal systemd sweep. The sweep returned `archived` for Issue #12. The original
configuration was immediately restored at SHA-256
`9bd4feaf06ec2c34a772ed89de56df8fddfea6f41676697ce0d07b65c20b0ae5`; automatic retention is
again absent.

Control's durable row is `archived`, binds expected HEAD
`41e67598b506dcbfeac00e5871a812e6e9874078`, and records 8,589,934,592 reclaimed bytes and Runner
time `2026-08-22T17:23:04.242245+00:00`. Runner's permanent tombstone matches the WorkItem and HEAD,
has metadata SHA-256 `12982f3e0487bf417907b1c870579a26dbffa3b89daf4045de5a27f7e1a89a8f`, and is `archived`. The final
image, image archive entry, workspace, and all staging entries are absent; the permanent registry is
retained as designed.

After reclamation, Issue #20 was reprovisioned at its original registry identity from the protected
backup. Its stable tree SHA-256 remained
`c48719d761e82425294f6fb408166b671cc84065996f745d1846759136b812ae`, its clean HEAD remained
`f5037925502905fd3d22a807df7291ba1004bab9`, and its replacement image is again a dense 8-GiB
regular file. The sensitive workspace backups were then deleted exactly; only a sanitized mode-`0600`
evidence JSON remains under Runner `run/`. A repeated ordinary sweep with retention disabled was
strict `idle`.

Post-canary Online Backup `state-20260822T172641.390390Z.db` was mode `0600`, passed integrity and
foreign-key checks at schema 12, and contained exactly one archived row and zero active Turns.
Runner had an available global lock, zero running containers, six final images, empty image/workspace
staging directories, and 22,545,313,792 available bytes (26.79%). Both timers were restored
active/enabled; the first resumed timer-triggered sweep at `2026-08-23 01:27:37 CST` was also strict
`idle`. No Issue label, PR, branch, merge, GitHub Actions run, Slack message, source release, network
policy, or credential changed during this canary.

The #12 workspace deletion is intentionally irreversible: restoring either SQLite backup does not
restore the reclaimed image. Control rollback remains possible for database or binary faults, and
Issue #20 was fully restored before timer activation, but there is no retained #12 workspace backup.

## Disposition, legacy archive, and completed backlog reclamation — 2026-08-23

The owner explicitly authorized review and merge of Fixture PRs #29, #31, #33, #36, and #37. Each
exact head had a successful GitHub Actions run, was Ready and mergeable-clean, and changed only one
README marker line. The operator-created fast-forward batch preserved every PR head as a merge
parent and advanced Fixture `main` from `7ee18770d9faec6845f1dc4e32082dcc595c2832` to
`54499f25f2be7892ec8379f98d7b431df07e42f2`; GitHub reports all five PRs merged. This was an
explicitly authorized fixture operation, not an automatic dispatcher merge.

Code release `3616b71794351e4e8164d15050189e87cbf7691d` added schema 13, audited
`abandoned`/`superseded` dispositions, completed-generation retirement, legacy-directory archive,
strict image/archive-staging classification, and read-only capacity evidence. Its exact Git archive
SHA-256 was `6fb064e3ef64143825346e1f636f2bbd4dad9989af63ffb20b35bef130fb885a`.
All 517 local tests, `compileall`, and `git diff --check` passed. Runner Python 3.12 also passed the
25 focused disk/workspace tests. A non-protected v1 tombstone test fixture initially exposed the
remote shell's permissive umask; test-only commit `3616b717` made its mode explicitly `0600` before
the release links moved.

The Dispatcher timer was stopped and the active oneshot was allowed to finish. Online Backup
`state-20260822T185953.695987Z.db` was mode `0600`. The exact backup copy migrated under the new
release before live mutation: schema 13, `integrity_check=ok`, zero foreign-key errors, zero active
Turns, zero completed live generations, and empty disposition/absence ledgers. Runner prechecks
found zero containers, zero image/workspace staging entries, and zero incomplete v1 tombstones.
Runner moved first, then Control. The live migration produced the same integrity result and retired
all historical completed generations at their durable completion event times.

The owner separately authorized Fixture Issue #20 as abandoned. GitHub event `29855076664`, actor
`longwdl`, changed its exact single state to `agent:discard`; no task-branch PR existed. One normal
sweep recorded the immutable `abandoned` disposition and a second normal sweep archived its clean
bounded image at HEAD `f5037925502905fd3d22a807df7291ba1004bab9`, reclaiming
8,589,934,592 bytes. The backward-compatible WorkItem state remains `blocked`, while the disposition
terminal overlay prevents any later Turn or generation.

The owner then authorized the six remaining historical review PRs for discard. Issues #2, #4, #6,
#8, #10, and #16 changed from `agent:review` to `agent:discard`; audited GitHub event ids were
`29861032691`, `29861033392`, `29861034200`, `29861034943`, `29861035613`, and `29861036319`.
Twelve serialized normal sweeps recorded six immutable `superseded` dispositions and archived the
six legacy directories, reclaiming 863,514 bytes. Their only generations are retired. The
backward-compatible WorkItem state remains `review`. PRs #3, #5, #7, #9, #11, and #17 remain open
drafts at their persisted branches and exact head SHAs; discard intentionally neither closes a PR
nor deletes its remote branch.

After Issues #28, #30, #32, #34, and #35 were projected to `agent:completed`, every reclamation
target was re-read as an exact merged PR at the persisted head. Under the Control process lock, an
explicit bounded operator batch archived only Issues #14, #18, #28, #30, #32, #34, and #35. #14
and #18 used v2 `legacy_directory` tombstones and reclaimed 144,260 and 145,445 bytes. The other
five used v2 `bounded_image` tombstones and each reclaimed 8,589,934,592 bytes. The batch did not
enable global completed retention and did not select any other WorkItem.

Issues #24 and #26 are known historical manually-missing Runner states. They consume no Runner
space and deliberately remain `completed` with no archive or absence-ledger row; this release does
not expose a local-JSON absence reconciliation command. Post-checks found schema 13 integrity `ok`,
zero foreign-key errors, zero active Turns, zero live completed generations, zero absence rows,
zero remaining images, zero staging entries, and zero containers. Runner available space increased
from 22,524,895,232 to 74,064,961,536 bytes; Turn and new-image admission both passed with zero
shortfall. Post-operation Online Backup `state-20260822T191713.622788Z.db` was mode `0600`, schema
13, `integrity_check=ok`, with zero foreign-key errors and zero active Turns. A normal post-deploy
sweep returned strict `idle`, and both Dispatcher and backup timers were restored active. Binary
rollback remains the previous `aef09f5` links, but schema rollback also requires the validated
pre-migration backup because schema 13 is additive and old code is unaware of the new terminal
overlay.

After the six superseded archives, a further normal sweep returned success without selecting more
work. Runner retained only Issue #1 under the fixture workspace root, with zero staging entries and
74,070,376,448 available bytes. Control still had zero active Turns and zero absence rows; Issues
#24 and #26 still had neither archive nor disposition rows. Online Backup
`state-20260823T012651.702413Z.db` passed schema-13 integrity and foreign-key checks with zero active
Turns. Dispatcher and backup timers were restored active, while the oneshot service was inactive.

## Completed WorkItem lifecycle and disk reclamation release — 2026-08-23

Commit `aef09f5ea9d4c78a9b8d86dedb972a2423274382` added the explicitly enabled completed-WorkItem
retention policy, schema-12 archive ledger, strict protocol-v2 `ARCHIVE`/`ARCHIVE_STATUS` recovery,
permanent Runner tombstones, exact workspace/image staging and reclamation, and new-Turn host disk
admission. Control durably records an ambiguous request before crossing SSH and treats a generic
Runner rejection as outcome-ambiguous because the protocol does not prove whether it occurred
before or after tombstone/staging effects. Only a strict status reply can authorize continuation.

The exact Git archive SHA-256 was
`66d1dddf27a091a614c8e45b77188d800638eca786dc26cd9c0c4bdeda5be044`. All 499 tests,
`compileall`, and `git diff --check` passed locally. Exact archive copies passed the same 499 tests,
compilation, and wrapper syntax checks as the real `codex-dispatcher` Python 3.14 and
`codex-runner` Python 3.12 accounts. The first target commands exposed only test-harness invocation
errors: the Control attempt used the administrator home instead of the service-owned staging root,
and the first Runner attempt was split by nested SSH quoting. Both temporary copies were cleaned;
the corrected service-owned, `umask 077` runs passed completely before either `current` link moved.
An independent Sol review found and verified fixes for tombstone replay, pre-SSH durability,
retention/config drift, registry filename/payload identity, and rejection-after-effect recovery; it
reported no remaining P0/P1.

Before switching, both hosts ran
`a52cdf86548aa6780f048e2f35c1755e3e03cdaf`. The Dispatcher timer was stopped and its current
oneshot allowed to finish naturally. SQLite returned `integrity_check=ok`, schema 11, zero active
Turns, and five completed WorkItems. Pre-migration Online Backup
`state-20260822T163027.788365Z.db` was mode `0600`, passed integrity at schema 11, and had zero
active Turns. The candidate read-only preflight was strict `idle` with `external_writes=false`.
The Control configuration deliberately omitted `completed_retention_seconds`, so installing this
release could not select a real WorkItem for deletion.

Runner prechecks found its global lock available, zero running rootless-Docker containers, zero
archive/tombstone staging entries, and 22,548,459,520 available bytes. Its candidate loaded the
real protected configuration as `rootless_docker` with the bounded WorkItem disk and trusted Sol
policy bundle. Runner `current` moved first and passed an empty-frame forced-command rejection
smoke test without changing configuration, policy, registry, workspace, or image state. Control
then moved to the same release. The first manually observed write-enabled sweep returned strict
`idle` and applied only additive migration 012.

Post-switch SQLite returned `integrity_check=ok`, zero foreign-key violations, schema 12, zero
archive records, zero active Turns, and the same five completed WorkItems. Runner still had zero
running containers and an available global lock. Post-migration Online Backup
`state-20260822T164226.306160Z.db` was mode `0600`, passed integrity at schema 12, and contained
zero archive records. Both Dispatcher and backup timers were restored active/enabled; the first
normal timer-triggered sweep at `2026-08-23 00:43:46 CST` was also strict `idle`.

No live WorkItem was archived in this rollout. Unit fault injection covers the protocol transitions
and exact-object deletion boundaries, but the real ext4 host was not crashed between individual
unmount/rename/unlink/fsync instructions. No Issue, PR, branch, GitHub Actions, Slack message,
merge, default branch, release tag, network policy, credential, or production repository changed.
Binary rollback requires stopping the Dispatcher timer and restoring both `current` links to
`a52cdf8`; because the live database is now schema 12, rollback to schema-11 code also requires
restoring the validated pre-migration backup rather than moving only the symlink.

## Completion gate, fresh Audit, and context-failure release — 2026-08-22

Commit `a52cdf86548aa6780f048e2f35c1755e3e03cdaf` added the durable protocol-v2
completion gate, mandatory fresh Audit generation, and fail-closed context/compaction failure
rotation. A `completed` implementation result now remains `published/running` until configured
exact-HEAD Actions checks, structured Issue acceptance predicates, and the verified publication
ledger pass. With `rotate_before_final_audit=true`, that pass creates a separate role=`audit`
generation and session; only its independently gated completion enters review. Context-failure
rotation requires a Runner receipt proving a clean worktree at the unchanged input HEAD. Dirty,
moved-HEAD, or unverifiable failure state blocks instead.

The exact Git archive SHA-256 was
`903c2ded60a86d5b274b9e1b476e315dc147d45d54206638ea696d6b3722f90d`. All 485 tests,
`compileall`, and `git diff --check` passed locally. The same 485 tests and compilation passed from
exact-byte writable staging copies under the real `codex-dispatcher` Python 3.14 and
`codex-runner` Python 3.12 accounts. The first target-host attempts exposed only staging metadata:
Git archive directories retained group-write bits and the interactive service-account shell used
`umask 0002`, so the existing protected-path tests rejected those fixtures as designed. Removing
group-write bits and using `umask 022`, matching the protected release premise, produced the two
complete passes. The immutable candidates were root-owned and recursively non-group/world-writable;
shell syntax and Control systemd unit verification also passed.

Before switching, the Dispatcher timer was stopped, both services were inactive, SQLite reported
`integrity_check=ok`, schema 9, and zero active Turns or WorkItems. Online Backup
`state-20260822T151233.431785Z.db` was mode `0600` and independently passed integrity at schema 9.
Both previous `e4c528ccd009940a15a248f5ffd18b116429250a` releases and the pre-change Control
configuration were preserved. Runner switched first, then Control enabled
`rotate_before_final_audit=true` and switched to the same exact release. The first new-release
oneshot returned strict `idle` and applied only additive migrations 010 and 011, leaving schema 11,
zero active work, and empty new evidence tables.

The live canary reused existing Fixture Issue
[`#34`](https://github.com/longwdl/codex-dispatcher-fixture/issues/34), WorkItem
`wi_2a2734d87231c0d6cae96c91`, Draft PR
[`#37`](https://github.com/longwdl/codex-dispatcher-fixture/pull/37), and its existing bounded Runner
disk. This avoided new disk admission while the host had approximately 22 GB free. The configured
generation budget was explicitly widened from 3 to 6 while `max_total_turns=10` stayed fixed. A
maintainer `/codex-context` requested a read-only verification and explicitly left CI/acceptance
judgment to the Dispatcher.

Generation 3 first rotated normally for existing context pressure into Implementation generation 4,
session `01a02a10-aeb0-7fa0-b765-220dee5ddbb0`. Turn
`turn_4974ec299a1c4afbbbdcb52465155bbc` completed without changing HEAD. Completion evidence
SHA-256 `d7fdfc5ea81c9e44ca2537b9e5b10f2348b05a7440343ef82ef819aeda20208e` bound the exact
published SHA, all three structured criteria, and successful Fixture Actions run
[`32573230842`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32573230842), then
passed. The mandatory `completion_candidate` Handoff retired generation 4 and created role=`audit`
generation 5 with new session `01a02a12-a047-7391-be2f-62b8e4c0e5a7`. Audit Turn
`turn_45c8c7bacfaf441893530510cd16ba39` independently completed without changes and passed a second
gate with evidence SHA-256
`c244a83a27373443d6b410eba059aedc238a203ba423e561dd6c8b247625f12c`. Both Turns also stored
policy-verified Sol delegation receipts; no prompt or model output was persisted in those receipts.

Independent GitHub and SQLite read-back found both gates and acceptance results `passed`, configured
check `fixture` passed, zero active Turns, and zero context-failure receipts. Issue #34 returned to
`agent:review`; PR #37 remained open and Draft at
`78000887b8e4a9c1979d9bac68e174df0cbd6091`. The task ref and sole Actions run stayed at that exact
SHA, while Fixture `main` remained `7ee18770d9faec6845f1dc4e32082dcc595c2832`. An immediate
repeated normal oneshot was strictly `idle` and retained six Turns, five generations, and two gates.
Post-canary backup `state-20260822T152810.147580Z.db` was mode `0600`, passed integrity at schema 11,
and contained both gates. Dispatcher and backup timers were restored active.

No merge, release tag, force-push, branch deletion, Issue close, default-branch update, production
access, Runner image creation, or network-policy change occurred. The canary live-proves the
completion gate and fresh Audit paths. Clean and dirty context-failure branches are covered by the
Runner/protocol/StateStore integration tests, not by destructive live fault injection. Binary or
configuration rollback requires stopping the Dispatcher timer and restoring both hosts together;
rollback to schema-9 code additionally requires the validated pre-migration backup rather than only
moving a release symlink.

## Trusted Sol delegation receipt canary — 2026-08-22

Commit `e4c528ccd009940a15a248f5ffd18b116429250a` made the Runner's primary
`gpt-5.6-sol`/`xhigh` thread responsible for selecting among the pinned Spark, Luna, Terra, and Sol
specialist profiles, restricted delegation to direct children, and added a metadata-only protocol-v2
receipt derived from the isolated Codex `state_5.sqlite` edge and token delta. The receipt verifies
the exact Codex version, role, model, reasoning effort, edge status, and per-Turn child token usage;
it contains no prompt, agent message, tool argument, or model output. Additive migration 009 stores
the canonical receipt and digest atomically with the terminal Turn.

The exact Git archive SHA-256 was
`2cc62b09b069c85067c0e3b67a9f0cb5ba74a915fa9b42bc00105354d447838a`. All 476 tests,
`compileall`, and `git diff --check` passed locally. The same 476 tests and compilation passed from
exact-byte writable staging copies under the real `codex-dispatcher` and `codex-runner` accounts on
their respective Python 3.14 and 3.12 runtimes. Directly running the whole suite from the immutable
Control release produced only nine known test-fixture write errors where tests intentionally create
temporary directories below their source root; no runtime assertion failed. Matching SHA-256 values
for the new observer, migration, and policy manifest were read independently on both hosts.

Before switching, the Dispatcher timer was stopped, both services were inactive, SQLite reported
`integrity_check=ok`, schema 8, and zero active Turns. Online Backup
`state-20260822T132740.489798Z.db` was mode `0600` and independently passed integrity at schema 8.
Both prior release/configuration/policy boundaries were preserved in root-only rollback directories.
The archive was initially staged under an incorrectly prefilled long-SHA directory name; this was
detected before either `current` link, configuration, or schema changed. Both candidate directories
were renamed to the actual full commit SHA above and the unchanged archive bytes were reverified.

The Runner switched first, installed policy digest
`ac698244f3546e13574118fe72b132250353f42d79d614fe059d445e5449a946`, and read back primary Sol
plus all four exact role/model/effort profiles through the production configuration loader. Control
then switched to the same release and digest. Its first normal sweep was strictly `idle`, migrated
only the additive schema 9 table, and left zero active Turns.

The live canary reused existing Fixture Issue
[`#34`](https://github.com/longwdl/codex-dispatcher-fixture/issues/34) and its existing bounded
Runner image. A maintainer context requested two independent read-only verification work packages
without selecting a model. Policy change retired generation 2 and created generation 3
`sg_2c074df0dae940058789eb57ef60f50f` through the normal `policy_changed` Handoff
`handoff_c6f91f00171d47d880be6617d86ad20a`. The exact-head Actions importer retained run
[`32573230842`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32573230842) at
checkpoint `78000887b8e4a9c1979d9bac68e174df0cbd6091`, and all three structured acceptance criteria
remained `passed`.

Primary session `01a029b1-9f5e-7353-89a7-fa2bda9de5a6` autonomously selected two different direct
children: `terra_worker` on `gpt-5.6-terra`/`medium` and `spark_worker` on
`gpt-5.3-codex-spark`/`medium`. Independent Runner state read-back matched the Control receipt:
43,298 and 109,834 child tokens respectively, both edges rooted directly at the Sol session, exact
Codex CLI `0.147.0`, and receipt SHA-256
`2404ee36db1a6be78b296a376f4067c921ff15848ac7f2d9e299baa1ac70267c`. Turn
`turn_fb971ef3814743c785712e89d11aaed3` finished `completed` with a clean worktree and unchanged
checkpoint. Issue #34 returned to `agent:review`; Draft PR
[`#37`](https://github.com/longwdl/codex-dispatcher-fixture/pull/37) stayed open and Draft at that
exact head, while Fixture `main` remained `7ee18770d9faec6845f1dc4e32082dcc595c2832`.

An immediate repeated normal sweep was strictly `idle`. Post-canary Online Backup
`state-20260822T134306.553406Z.db` was mode `0600`, passed integrity at schema 9, and contained the
single delegation receipt. Dispatcher and backup timers were restored active. Binary/config rollback
requires stopping the Dispatcher timer and restoring both hosts' prior `current` links and policy
digests together; rollback to schema-8 code additionally uses the validated pre-migration backup.
No merge, release tag, force-push, branch deletion, Issue close, default-branch update, production
access, network-policy change, or unrelated WorkItem mutation occurred.

## Rejected PREPARE reactivation fix — 2026-08-22

Commit `c6ba48e31a914919bf3324c9a54bd43be6f9b17e` fixes the fail-closed reactivation gap exposed by
Fixture Issue #34. Control now treats the durable `preparing -> ready` event as PREPARE ACK
provenance. A blocked or paused WorkItem without that provenance loads an exact source bundle from
its persisted Base SHA before GitHub claim, returns to `preparing`, and retries idempotent PREPARE;
START is possible only after the new acknowledgement is durable. A later Turn-blocked WorkItem with
existing provenance continues directly through normal reactivation and is not re-prepared.

The regression reproduces `PREPARE(rejected) -> PREPARE -> START -> RESUME`, also proving that an
exact-source failure occurs before another claim and that ordinary blocked-Turn recovery performs no
additional source read or PREPARE. All 468 tests and `compileall` passed locally. The root-owned
staged Control release passed the 81 directly related tests as the real `codex-dispatcher` account
under the service Python 3.14 runtime, shell syntax checks, matching local/remote SHA-256 checks for
the changed runtime files, and systemd unit verification. The release archive SHA-256 was
`e47b7820270c0bb427f656a5e9b80aada24ad017dc0f4cf3f8689038dbb0fc9e`.

Before the switch, SQLite returned `integrity_check=ok` with zero active Turns, and Online Backup
`state-20260822T111750.956151Z.db` was mode `0600` and passed its own integrity check. Control
`current` moved atomically from `c7d3e0b40194a72990d5828f81fbc463a19997f5` to `c6ba48e`. The
new-release read-only preflight returned strict `idle` with `external_writes=false`; the following
normal oneshot also returned `idle`. Post-switch SQLite still had zero active Turns, Online Backup
`state-20260822T112301.390824Z.db` was mode `0600` and valid, and both Dispatcher and backup timers
were active.

Live durable state distinguishes the original rejected attempt from a prepared WorkItem: Issue #34
remains `blocked` with `prepare_ack=0`, while successful Issue #35 remains `review` with
`prepare_ack=1`. Issue #34 was deliberately not reactivated during this rollout. The Runner had
31,155,351,552 free bytes; creating its expected approximately 8-GiB WorkItem image would drop the
host below the configured 25-GiB new-admission boundary and block subsequent new fixture work. No
Runner release, protocol, configuration, workspace, GitHub Issue, branch, pull request, or Slack
state changed during this fix rollout. Binary rollback is the previous Control symlink; there was
no schema migration.

## Exact-head Actions evidence and structured AC fixture — 2026-08-22

Commit `c7d3e0b40194a72990d5828f81fbc463a19997f5` added the bounded GitHub Actions
evidence importer, strict structured acceptance predicates, schema-2 Handoff writer, and
schema-1 Handoff reader compatibility. The source archive SHA-256 was
`a0a98d98ec33d56986ee7f52a8d6ee9a5f1ea2842bcf0ea1b0913425b79b7ee9`. All 463 tests and
`compileall` passed locally. The root-owned Control Host candidate additionally passed the 43
directly affected tests as the real `codex-dispatcher` account, read-only bytecode compilation,
shell syntax checking, and systemd unit verification. Attempts to run the complete suite from
immutable or unusually long target-host test roots exposed only test-fixture ownership and Unix
socket path assumptions; they were not counted as passing target-host runs. No Runner source or
wire-protocol file changed, so the Runner release was deliberately left unchanged.

Before activation the system Dispatcher timer and service were stopped, SQLite had no active Turn,
and read-only preflight was strictly `idle`. Online backup
`state-20260822T092213.792891Z.db` was mode `0600` and passed `integrity_check`. The Control Host
`current` symlink was then atomically moved from release
`3e709aea68c1cb4d17991002b10d4923d4f45840` to the exact `c7d3e0b` release. A new-release
read-only preflight again returned `idle` without an external write. There was no SQLite schema
migration; database schema remained 8.

The first canary attempt, Fixture Issue
[`#34`](https://github.com/longwdl/codex-dispatcher-fixture/issues/34), proved two independent
fail-closed boundaries before an Agent ran. PREPARE first rejected because the Runner had only
22,566,002,688 bytes available while an 8-GiB WorkItem image plus the configured 16-GiB host
reserve required 25,769,803,776 bytes. After one explicitly selected completed fixture image was
retired, a generic blocked-to-ready reactivation sent START without repeating the rejected PREPARE;
the Runner rejected the missing registry/image identity and Control retained the Turn as
`runner_request_rejected`. Issue #34 remains `agent:blocked` with no task branch, PR, checkpoint, or
Actions run. This is an existing preparation-retry state-machine gap, not evidence against the new
importer, and must be fixed separately without weakening Runner admission.

The clean canary used Fixture Issue
[`#35`](https://github.com/longwdl/codex-dispatcher-fixture/issues/35), WorkItem
`wi_dbe8e403948af08923dfad91`, task branch `codex/issue-35-dbe8e403948a`, and Draft PR
[`#36`](https://github.com/longwdl/codex-dispatcher-fixture/pull/36). During the manually isolated
test only, `max_turns_per_session` was changed from 4 to 1 so the second reviewed context had to
rotate; the original config was restored byte-for-byte before normal scheduling resumed. Turn 1
`turn_80cffd7084104a838fb997575d3a1fe2` finished `needs_input` and published only `README.md` at
checkpoint `6da05673e416799d8327f8b0664f811cf657c20e`. GitHub Actions run
[`32565897345`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32565897345) was the
unique `pull_request` run named `fixture` for the exact task branch and exact checkpoint, and
finished `completed/success`.

After the maintainer supplied `/codex-context finalize`, the one-Turn budget retired generation 1
`sg_c88dd25e1b5b4b6cadc9d320a59a8f96` and created generation 2
`sg_3669f64043734fb48f50a1bc14a9133c`. Before that transaction committed, the importer read the
exact remote task ref on both sides of the bounded Actions listing. Handoff
`handoff_c7fbcd52902f4e598cf7744a447ab7d9` persisted schema version 2 with provider
`github_actions`, exact HEAD `6da05673e416799d8327f8b0664f811cf657c20e`, exact run ID
`32565897345`, required check `fixture=passed`, aggregate acceptance `passed`, and zero remaining
items. All three issue predicates passed from dispatcher-owned evidence:
`required-check: fixture`, `changed-paths-within-allowed`, and `task-head-published`. Agent output
remained only in the separate untrusted advisory.

Turn 2 `turn_7433e51213514bf898bca2ba840c3d73` started from the same published checkpoint in the new
generation, made no further change, and finished `completed`. Issue #35 reached `agent:review`; PR
#36 remained open and Draft at the exact checkpoint; its single Actions run remained successful.
Fixture `main` remained `7ee18770d9faec6845f1dc4e32082dcc595c2832`. No merge, release, tag,
deployment, force-push, branch deletion, workflow edit, or default-branch update occurred.

Two explicitly selected completed fixture Runner workspaces, Issues #24 and #26, were unmounted and
their individual 8-GiB images plus Runner registry files were deleted after proving the global lock
available and zero running containers. Their GitHub assets and complete Control SQLite audit remain;
their local Runner workspaces are not recoverable. This restored 31,155,769,344 bytes of free space,
above the existing admission boundary, while preserving every review, blocked, and active canary
workspace. Rollback of the Control binary is to stop the timer and point `current` back to
`3e709aea`; because schema stayed at 8, no database restoration is required for binary rollback.
The restored timer's first real sweep was strictly `idle`, and both the Dispatcher and backup
timers were enabled and active. Post-canary Online Backup
`state-20260822T095716.230081Z.db` was mode `0600`, passed `integrity_check`, and contained the exact
schema-2 Handoff.

## Structured Handoff and fresh-session Bootstrap fixture — 2026-08-22

This checkpoint deployed schema 8 and exercised a real context-pressure replacement from
SessionGeneration 2 to 3. It proves the structured Dispatcher/Git Handoff, explicit untrusted
advisory boundary, first-Turn binding, fresh-session Bootstrap, and unattended recovery behavior
against the dedicated private Fixture. It does not claim CI or acceptance evidence that the
Dispatcher cannot mechanically observe.

Commit `d89d73a7b9f712422b73f0dfab7832d76e26d179` added canonical Handoff persistence, complete
AgentResult receipts, verified publication checkpoints, the generation-first-Turn Handoff binding,
and the Bootstrap contract. Its Git archive SHA-256 was
`18e98960121b1eda7b2a0439b22650f4eeac93df68ef36547080dfdb1e69e3dc`. Root-owned candidates were
installed on both hosts, normalized to remove group/world write permission, and tested from
service-owned mode-`0700` copies with `umask 077`. All 449 tests and `compileall` passed independently
under both the `codex-dispatcher` and `codex-runner` accounts.

Before activation, the Dispatcher timer was stopped, the service was inactive, SQLite had no active
Turn and returned `integrity_check=ok`, the Runner global lock was available, and read-only preflight
was strictly `idle`. The mode-`0600` schema-7 Online Backup
`state-20260822T080447.699639Z.db` passed integrity checking and contained no active Turn. Runner and
Control Host `current` then moved to the exact candidate. New-release preflight migrated only its
disposable snapshot and left the source database at schema 7. The following manually observed idle
sweep additively migrated the source to schema 8; `integrity_check`, `foreign_key_check`, active-Turn
count, and all four new tables were clean. Restoring the schema-7 backup together with the prior
`df707c5` releases remains the old-binary rollback path.

Private Fixture Issue
[`#28`](https://github.com/longwdl/codex-dispatcher-fixture/issues/28) and Draft PR
[`#29`](https://github.com/longwdl/codex-dispatcher-fixture/pull/29) already had one active generation
2 Turn. One reviewed maintainer context created Turn 4
`turn_4890f0e336994ec69938543bcef5928e` in the same generation. It used 160,440 input tokens,
finished `completed`, and published checkpoint
`c6156bcb51d2fd872fa20f7c367ab4caa9127cfb`. Schema 8 atomically retained its complete AgentResult
receipt and publication evidence: bundle SHA-256
`4ef31a8bdd2081ee588163226acb98dfed3ee12361205f4e54784e447771e0fc`, exactly `README.md`, one
commit, and 15,605 bytes. Actions run
[`32561639445`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32561639445)
completed successfully for that exact head.

The next reviewed context encountered the configured context-pressure threshold before another
Turn. In one SQLite transaction the Dispatcher retired generation 2, planned generation 3
`sg_66642add15ec4cf38a559411826d2dfb`, and inserted Handoff
`handoff_f007e71932114bff9d4c23a979312359` with SHA-256
`333cda1f2bfb1b9341a308713d36f4c676c02798b9acac3ffe1401d5963bd739`. The immediately following
Full START created Turn `turn_3dcb2ba461b8451bae2e9894c2115edb` and durably bound that exact
Handoff; no Delta Turn can replay it.

The Handoff's trusted facts named the exact WorkItem, Issue revision, branch, base, source and target
generations, current head `c6156bcb51d2fd872fa20f7c367ab4caa9127cfb`, and the verified
`README.md` path. Acceptance remained `unverified` with no invented evidence, and required check
`fixture` remained `not_observed`. `publication_evidence_complete=false` truthfully recorded that
the schema-8 checkpoint ledger does not backfill the earlier schema-7 prefix. The complete Turn 4
AgentResult was present only under `untrusted_advisory`; a decoded provenance check proved its
summary matched the source receipt and was absent from every trusted-fact string.

Turn 5's generation-3 Bootstrap verified the branch, prior head, clean worktree, recent history, and
README-only scope before editing. Its summary explicitly identified the old advisory's “no further
local action” as stale and superseded by the new reviewed instruction. The Turn finished
`completed`, with a distinct Codex session, at checkpoint
`f6e450d5ec60d866e8cd20f17ee97ebfe194598d`. The Runner's mode-`0600` durable record was `finished`,
bound generation 3 and the same checkpoint, and had no error code. The Dispatcher retained a second
complete AgentResult and publication checkpoint. Actions run
[`32561861958`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32561861958)
completed successfully for the final head.

The final Issue-state verification exposed an independent long-Timeline boundary: the complete
Issue Timeline had grown to 66,198 bytes, just over the existing 65,536-byte `gh` output cap. The
review label, status comment, publication, Slack receipt, local terminal state, and Runner terminal
record had already succeeded, but verification correctly failed closed with
`gh command output was truncated`. No Prompt was replayed. Commit
`3e709aea68c1cb4d17991002b10d4923d4f45840` retained the same 64-KiB boundary and used fixed `gh
--jq` projection to emit only the required ready-label event, timestamp, actor login, and label
name. The exact live projection was 575 bytes. Adapter, recovery, preflight, all 449 local tests, and
all 449 tests on both Linux hosts passed before both releases moved to this commit. Its Git archive
SHA-256 was `d2395b2ce425b206f1ad49fd18dce98a1f926407d915be439427b88fdb389499`.

Post-fix read-only preflight, one manually observed sweep, and the first timer-triggered sweep were
all strictly `idle`. Final SQLite schema 8 returned `integrity_check=ok`, zero foreign-key
violations, and zero active Turns; Issue #28 had three generations, five Turns, two schema-8
AgentResults, two publication checkpoints, one Handoff, and one first-Turn binding. All six Slack
outbox records for the WorkItem were delivered to the configured Fixture channel, including the
Turn 4 and Turn 5 replies. The Issue was `agent:review`; PR #29 remained open and Draft, its five
commits changed only `README.md`, and its final head matched the task branch. Fixture `main` remained
`7ee18770d9faec6845f1dc4e32082dcc595c2832`.

Both hosts now run release `3e709aea68c1cb4d17991002b10d4923d4f45840`; the Dispatcher and
backup timers are enabled and active. Post-canary backup
`state-20260822T083044.925189Z.db` is mode `0600`, passed `integrity_check`, and contains the exact
Handoff. Immediate rollback is to stop the Dispatcher timer and restore both `current` symlinks to
`d89d73a`; that release reads schema 8. Rollback to `df707c5` additionally requires restoring the
validated schema-7 backup. No credential, Prompt, private key, full Runner output, merge, release,
tag, force-push, branch deletion, Fixture-main update, sshd change, firewall change, or higher-value
repository admission occurred.

## Per-WorkItem auth isolation and unattended completion fixture — 2026-08-22

This checkpoint deployed the independently writable per-WorkItem Codex authentication boundary,
proved it with a deliberate two-Turn private Fixture, removed the temporary Runner maintenance
grant, and enabled the Fixture-only production Dispatcher timer. The configured repository set
remained exactly `longwdl/codex-dispatcher-fixture`; this evidence does not admit a higher-value
repository.

Commits `cba1662` and `fc3850f` first fixed two offline safety boundaries. SQLite Online Backup now
removes its private staging WAL/SHM files after either success or failure without deleting an
already published backup. The Runner now treats `/srv/codex-runner/app/auth.json` only as a
host-owned seed: each WorkItem receives its own mode-`0600` copy below
`runner-state/codex-home/auth.json`, while a container-invisible mode-`0600`
`runner-state/codex-auth-binding.json` binds the immutable WorkItem identity to the seed digest.
START may initialize only an empty or auth-only bound home; RESUME requires the existing binding,
session, directory, branch, and tool identities to agree and otherwise fails closed. Neither the
shared seed nor the host-only binding is mounted into the container as a separately writable file.

The exact source commit `fc3850f85e5492633b420533475f67535264bae7` was pushed to `main` and
packaged only with `git archive`; its archive SHA-256 was
`506a345bda024b015ac15a90c9c636a81f571b38c1446814d00230b29b3097a8`. Root-owned candidate
releases were installed at
`/opt/codex-dispatcher/releases/fc3850f85e5492633b420533475f67535264bae7` on `s2` and
`/srv/codex-runner/releases/fc3850f85e5492633b420533475f67535264bae7` on `s3`. Each candidate
passed all 395 offline tests and `compileall` as its production service account. An initial
interactive test attempt inherited `umask 0002` and correctly caused three Publisher protection
tests to reject mode-`0775` temporary mirrors; rerunning inside protected service-owned temporary
roots with the production `umask 0077` passed 395/395 on both hosts. No assertion or security check
was weakened.

Before switching, `s2` SQLite returned `integrity_check=ok`, had no active run, and read-only
`ssh-preflight` was strictly `idle`; the Dispatcher timer was disabled. The `s3` Runner lock was
acquirable, rootless Docker had zero containers, and the host auth seed metadata and digest were
recorded without printing its contents. The `s3` and then `s2` `current` symlinks were atomically
switched to the exact candidate. Post-switch Runner imports, lock, daemon, container count, auth
seed digest, `systemd-analyze verify`, and another read-only preflight all passed. The prior releases
remain available at
`/srv/codex-runner/releases/df1654b280011e0a0696f598af66c4c0d8f195fd` and
`/opt/codex-dispatcher/releases/d7753fbbe2bae2ea3a16fa08c6114ad0b6c91ba8`.

Four new mode-`0600` SQLite Online Backups were independently checked with
`integrity_check=ok`: one immediately after the release switch, one before each live Turn, and one
after the Draft PR reached review. The backup directory already contained 34 historical
`.state-backup-*.tmp-{wal,shm}` files from the previous implementation. Their count remained exactly
34 after every new backup, proving that the fix created no new staging sidecar; the old files were
not deleted. The latest manual backup predates the later human merge and therefore records the
review state rather than the completed tombstone. The enabled network-isolated daily backup timer
remains responsible for subsequent scheduled backups.

Private Fixture Issue
[`#26`](https://github.com/longwdl/codex-dispatcher-fixture/issues/26) began with only
`agent:ready`, `exec:ssh-cli`, and `priority:p1`. Its strict task specification allowed only the
single README fixture line to change and deliberately withheld the exact replacement until a
reviewed maintainer context comment. Before the first sweep, SQLite had no Issue `#26` WorkItem or
Turn, the deterministic Runner directory did not exist, the task branch and matching PR did not
exist, Fixture `main` was `48cd71d95b7484a6fa1db65495eec16ab63c1bed`, a fresh backup passed,
and preflight selected only Issue `#26` as `ready_candidate`.

Turn 1 created exactly these stable identities:

- WorkItem `wi_6bee727d623ec61a2d31cf11`;
- branch `codex/issue-26-6bee727d623e`;
- Runner directory
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-26`;
- Codex session `01a02707-9162-7211-bc79-52e8cfc396f4`;
- Turn `turn_56c06e6f1a40411f86d3233c67d124b0`.

It finished `needs_input` with identical input/output HEAD, no changed path, publication SHA,
branch ref, or PR. SQLite moved the WorkItem to `waiting_input`; GitHub projected only
`agent:needs_input`; Slack contained one delivered root and one delivered question. The WorkItem
auth copy, auth binding, and session binding were regular `codex-runner`-owned mode-`0600` files
with link count one. Both auth JSON files parsed, the binding contained only its version, exact
WorkItem, and original seed digest, the copy initially matched the seed digest, and the shared seed
metadata and digest were unchanged.

Maintainer `longwdl` then added exactly one reviewed context comment with immutable node ID
`IC_kwDOT3NfX88AAAABQH6NZA` and returned the Issue to the canonical `agent:ready` state. A second
fresh backup passed and preflight again selected only Issue `#26`. The next normal sweep created
Turn `turn_72929f40ee6a4796a4a4bc2e1cda134d` and used RESUME: it reused the original WorkItem,
branch, Runner directory, Codex session, auth copy, auth binding, session binding, and base HEAD. It
did not PREPARE or START a replacement identity and included only the reviewed comment ID.

Turn 2 finished `completed` at checkpoint
`2d7a71747d2ca11291fae8f4dd64145b8818948d`. Independent Runner STATUS requests over separate SSH
connections returned both Turns as `finished` with the same session: Turn 1 retained its unchanged
HEAD and `needs_input` result, while Turn 2 returned the checkpoint and only `README.md` in changed
paths. The host auth seed digest remained unchanged; the WorkItem copy and both host-only bindings
remained valid and mode `0600`; exactly two durable Runner Turn records existed.

The Dispatcher published only that checkpoint and created one Draft PR
[`#27`](https://github.com/longwdl/codex-dispatcher-fixture/pull/27). SQLite contained one WorkItem,
two ordered Turns, one PR binding, and three delivered Slack records: the original root, Turn 1
question, and Turn 2 result. The PR had base `main`, the deterministic task branch as head, exactly
one commit, and only `README.md` with one insertion and one deletion. The resulting marker matched
the reviewed context without recording the Prompt in this evidence. GitHub Actions run
[`32543112592`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32543112592)
completed successfully for the exact checkpoint. Fixture `main` remained unchanged, repeated
write-enabled sweep and read-only preflight were both `idle`, and no second WorkItem, Turn, session,
branch, PR, Slack root, or workflow run appeared.

After human review, the maintainer merged PR `#27`; the Dispatcher did not perform the merge. GitHub
recorded merge commit `7ee18770d9faec6845f1dc4e32082dcc595c2832` on `main`. The next
inactive-relative timer sweep returned `completed` for the exact Issue and WorkItem, moved the
durable WorkItem to `completed`, and projected the still-open Issue to `agent:completed` while
retaining exactly two comments. Counts remained two Turns, one session, one PR, and three Slack
deliveries. The following read-only preflight and the next automatic timer sweep were both `idle`;
SQLite still returned `integrity_check=ok` and Actions remained successful at the original PR head.

Only after the live Turn, PR, Actions, repeated-idle, and auth checks passed was the exact temporary
Runner sudo rule `/etc/sudoers.d/90-codex-maintenance` copied to the root-only mode-`0600` rollback
file
`/root/codex-runner-rollback-fc3850f-20260822/90-codex-maintenance`. `visudo -cf` accepted the
rule before removal and the complete sudoers configuration afterward. A new SSH connection proved
`sudo -n` unavailable; `codex-runner` remained in only its own group, and a later forced-command
STATUS still returned the completed Turn through the production Runner path.

Finally, `codex-dispatcher.timer` was enabled and started with the reviewed 120-second
inactive-relative schedule. Its first two observed automatic sweeps were `idle`; after the human
merge a later automatic sweep recorded `completed`, and the immediately following automatic sweep
returned `idle`. The Dispatcher and backup timers remain enabled and active, both hosts remain on
release `fc3850f85e5492633b420533475f67535264bae7`, SQLite is intact, and the 34 preserved legacy
backup sidecars remain unchanged. Rollback is to disable the Dispatcher timer, restore the prior
`current` symlinks, preserve SQLite/Runner state for reconciliation, and have root restore the
validated sudo rule only if maintenance access is explicitly required. No credential, private key,
Prompt, complete Runner output, automatic merge, force-push, GitHub Release, tag, Dispatcher branch
deletion, sshd change, firewall change, or higher-value repository admission occurred.

## Linux Control Host systemd artifact validation — 2026-08-20

The repository's fixed Control Host wrapper, `Type=oneshot` service, inactive-relative timer, and
root-only environment-file template were validated without installing or activating them. Five
offline invariant tests proved the fixed service user and argv, absence of fixture/preflight entry
points, explicit write gate template without credential values, protected state/runtime write
boundary, empty capability sets, argument-free wrapper, and non-overlapping timer shape.

All 324 offline tests passed. `sh -n`, `compileall`, and `git diff --check` also passed. A temporary
credential-free copy was transferred to a `mktemp` directory on the dedicated Linux Fixture host,
where systemd 255 accepted both units with `systemd-analyze verify --recursive-errors=no`. The same
temporary service scored `2.8 OK` under offline `systemd-analyze security`; the remaining exposure
was principally the required Internet/Unix sockets plus syscall/IP filters intentionally deferred
until the actual Control Host is selected. The temporary directory was removed afterward. No unit
was installed, no manager reload/start/enable occurred, and no Dispatcher, GitHub, Slack, Runner,
Publisher, branch, PR, merge, deployment, or release action was invoked.

The serviceization checkpoint then added a credential-free SQLite Online Backup command plus a
network-isolated daily oneshot/timer. Six additional offline tests exercised a real migrated state
database, source and backup integrity checks, mode-`0600` atomic publication, collision refusal,
source-symlink and weak-directory rejection, failed-staging cleanup, CLI assembly without tokens,
and the backup unit/timer invariants. The full suite passed at 330 tests.

All four dispatcher/backup units and timers passed the same temporary Linux systemd 255 verification
without installation. The backup service scored `2.1 OK` in the offline security audit. Its timer
was not enabled; no real Control Host database or backup path was opened, no credential environment
was loaded, and no existing backup was deleted.

## Linux Control Host production initialization and activation — 2026-08-20/21

The dedicated `s2` Control Host was initialized to the reviewed production filesystem and ownership
boundary at checkpoint `2a1be9dd127e08acc4b6460e7a781d5c47ba584c`. A root-owned CPython 3.14.7
was built from the official source checksum below `/opt/codex-python`, without replacing Ubuntu's
system Python. The immutable Dispatcher release, atomic `current` symlink, no-login
`codex-dispatcher` account, protected `/etc/codex-dispatcher` configuration and environment,
mode-`0600` Runner identity and known-host pin, mode-`0700` mutable roots, migrated SQLite state,
and four root-owned system units were installed. Credential values, private-key contents, Prompts,
and full Runner output were not printed or recorded.

The target host passed all 336 offline tests under the service account with the production Python,
minimal environment, `umask 077`, and service-owned temporary directory. Target `compileall`,
configuration loading, SQLite `integrity_check`, and `systemd-analyze verify` also passed. The loaded
Dispatcher and backup services scored `2.8 OK` and `2.1 OK`, respectively. One manually started,
credential-free, network-isolated backup oneshot created a service-owned mode-`0600` 147,456-byte
snapshot; its integrity was `ok` and its WorkItem/Turn counts matched the source database.

A transient service-account `ssh-preflight` loaded the same protected GitHub credential through
systemd, verified the exact Git, `gh`, and OpenSSH pins, and returned `idle`,
`external_writes=false`, and `authorizes_apply=false`. The configured state database SHA-256 was
unchanged before and after preflight. The command did not connect to the Runner or perform a GitHub,
Slack, branch, PR, merge, deployment, release, or tag write.

Initial activation failed closed at the documented host-capacity gate. The host exposed 1 vCPU,
980,152 KiB RAM, no swap, and a 20,747,476,992-byte root filesystem with 12,606,046,208 bytes
available, below the production minimum of 2 vCPU, 4 GiB RAM, and 50 GiB SSD. The write-enabled
service and both timers therefore remained inactive until the maintainer explicitly directed
activation without expanding the host on 2026-08-21. This is an accepted operating exception, not
evidence that the host meets the production baseline; memory exhaustion and disk pressure remain
open risks.

Before the exception was applied, a fresh protected backup again passed `integrity_check`, bringing
the retained backup count to two. SQLite had no active run or Dispatcher lock, and a new read-only
preflight again returned `idle` with an unchanged database SHA-256. One manually observed
`codex-dispatcher.service` start then returned `status=idle` and exited successfully. Independent
SQLite status and another read-only preflight remained idle and unchanged.

Both system timers were then enabled and started. Starting the Dispatcher timer immediately caused
one expected recovery-first sweep because its boot-relative deadline had already passed; that sweep
also returned `idle` and exited successfully. The Dispatcher timer was active with its next
inactive-relative sweep scheduled two minutes later, while the network-isolated backup timer was
active with its next daily run scheduled for the following calendar day. Final SQLite integrity and
GitHub preflight were unchanged and idle. No WorkItem, Turn, Issue, Runner, Slack, branch, PR,
Action, merge, deployment, release, or tag write occurred during activation.

Exact local and remote deployment staging directories, build scripts, and build logs were deleted
after verification; no installed release, configuration, state, or backup was removed. Emergency
rollback remains stopping and disabling both timers while preserving the database, backups,
mirrors, Runner state, branches, and pull requests for reconciliation.

## Runner dedicated account and root-owned SSH boundary — 2026-08-21

The dedicated `s3` Runner was migrated from the interactive `ecs-user` account to the locked,
non-sudo, no-supplementary-group `codex-runner` protocol account. Before mutation, the Control Host
timer was disabled, the service was inactive, a new mode-`0600` SQLite Online Backup passed
integrity validation, read-only preflight returned `idle`, no local active run or exact Runner/Codex
process existed, and the Runner global lock was acquirable. Five Codex SQLite databases returned
`quick_check=ok`; 10 WorkItems, 10 session files, and 13 finished remote Turn records were recorded
without reading credential, session, Prompt, or result contents.

Offline commits `219e084`, `d9f2338`, and `dcadba6` defined the ownership, fixed Docker planning, and
trusted-parent-chain contracts. All 345 tests, `compileall`, JSON parsing, wrapper syntax, and diff
checks passed locally and again on Ubuntu with the production `umask 077`. Runtime release
`dcadba6dd8d7b44c28a684901bf2b2bc4568c3a5` was installed root-owned. The complete Codex 0.147.0
distribution was copied from the administrator-owned Linuxbrew tree into the versioned root-owned
`/srv/codex-runner/tools/` tree with an identical binary SHA-256 and version result. This prevents a
non-sudo administrator account from replacing the configured executable through a writable parent.

The Runner root, releases, tools, wrapper, Schema, configuration, current symlink, and sshd drop-in
are now root-owned. Only `app`, `run`, `work-items`, and `/var/lib/codex-runner/home` are writable by
`codex-runner`. The protected config loads under the new account, ChatGPT login status remains valid,
all five Codex SQLite checks still pass, and the WorkItem/session/finished-Turn counts are unchanged.
The external authorized-key file is `root:codex-runner` mode `0640`: an initial root-only mode
correctly failed authentication because privilege-separated sshd could not read it, so the contract
was fixed in `28e140d` without making the public key writable or relaxing sshd. `sshd -t` and the
effective Match configuration proved public-key-only authentication, the exact force command, and
disabled password, keyboard-interactive, TTY, forwarding, tunnel, agent, X11, and user-rc features.
Only `reload` was used, and a separately held `ecs-user` administrator session survived it.

Before changing the Control Host username, an explicit `codex-runner` connection returned the
existing WorkItem `wi_9eb14638cf6691e8b2a783bb` and Turn
`turn_54ec2bff87594f53bb668ae8bf950bc1` as `finished` through STATUS with no artifact. After the
single protected TOML field was atomically changed, a configuration-derived STATUS returned the same
identity and state. It did not START/RESUME, resend a Prompt, create a session, or export a bundle.

Two manually observed sweeps, final read-only preflight, and the first timer-triggered sweep were all
`idle`. Control SQLite remained `integrity=ok` with no active run; its Slack table retained four
delivered records. The private Fixture remained on default branch `main`, with six existing open PRs;
its five most recent accessible Actions runs were all completed successfully. Runner ownership had
no exceptions, its lock remained acquirable, and no exact Codex process remained. The Dispatcher
timer was re-enabled, the exact temporary release staging directory was removed, and rollback copies
of both Runner and Control Host configurations plus the old wrapper and releases were retained. The
temporary `ecs-user` NOPASSWD bootstrap rule was moved out of the sudo include directory into a
root-only mode-`0600` rollback file; a new ordinary session proved passwordless sudo was no longer
available.

This completes the account, immutable-input, and SSH authorization boundary only. Docker is still
absent on `s3`, as are the rootless helper binaries and user manager. Cgroup v2 and subordinate ID
ranges are available, but host output/forward policy is permissive, private and cloud-service routes
exist, and WorkItems remain on ext4 without project quota. Per-WorkItem auth/session storage, an
allowlisted egress path with internal/metadata denial, and a hard aggregate disk limit remain
mandatory gates before container activation or admission of higher-value repositories.

## Runner CODEX_HOME isolation migration — 2026-08-20

The dedicated `s3` Fixture Runner moved its shared Codex-managed state from
`/srv/codex-runner` to the protected `/srv/codex-runner/app` directory. The business directories
`bin`, `current`, `etc`, `releases`, `run`, and `work-items` remained at the Runner root. No release,
task repository, Runner protocol state, or forced-command path moved.

Migration began only after a read-only Dispatcher preflight returned `idle`, no Codex or Runner
process was active, the global Runner lock was acquired, five Codex SQLite databases returned
`quick_check=ok`, and the source and destination were confirmed to be on the same filesystem. The
first guarded attempt failed closed before any move because the SQLite checks had materialized
additional WAL/SHM sidecars; the root, configuration, and absent destination were independently
verified unchanged. The exact allowlist was extended only for those Codex-owned sidecars.

The successful attempt atomically renamed 28 exact Codex-owned entries while holding the global
lock, atomically changed only `codex_home` in the protected Runner configuration, and retained the
mode-`0600` rollback copy
`/srv/codex-runner/etc/config.json.pre-codex-home-app-20260820T123232Z`. Post-checks found only the
six business entries at the Runner root, `/srv/codex-runner/app` at mode `0700`, `auth.json` at mode
`0600`, all five SQLite checks still `ok`, and the original session-file count unchanged. Credential
and session contents were never read or printed.

With a minimal environment, `CODEX_HOME=/srv/codex-runner/app codex login status` reported the
existing ChatGPT login. A real forced-command `STATUS` request then returned the existing fixture
Turn as `finished` with its original WorkItem, Turn, directory, and Codex session binding. It did not
start or resume Codex, resend a Prompt, export an artifact, or perform a GitHub, Slack, branch, PR,
merge, deployment, or release write.

After the example configuration and current architecture/operations documentation were updated,
all 319 offline tests passed. `compileall`, example JSON parsing, `git diff --check`, and a
credential-pattern diff scan also passed. A final read-only Dispatcher `ssh-preflight` returned
`ok=true`, `status=idle`, and `external_writes=false` with no selected Issue, WorkItem, Turn, or
recovery action.

## Runner audited egress production activation — 2026-08-21

The dedicated `s3` Runner now enforces audited outbound access for the host-level `codex-runner`
account. Before mutation, the `s2` Dispatcher timer and service were inactive, the backup timer
remained active, a new mode-`0600` SQLite Online Backup returned `integrity_check=ok`, and a
transient service-account `ssh-preflight` returned strict `idle` without external writes. The
Runner had no exact Runner/Codex process or pre-existing outbound socket, its global lock was
acquirable, port 3128 was unused, UFW was inactive, and the existing nftables/iptables/ip6tables
state plus Runner configuration and release target were copied into the checksummed root-only
rollback directory.

Commits `66c271d` through `e44b66b` added the standard-library policy validator, canonical
credential-free proxy configuration, complete Squid policy, metadata-only audit format, protected
tmpfiles/logrotate rules, and the host-specific nftables OUTPUT table. Every checkpoint passed all
363 offline tests, `compileall`, and `git diff --check`; the final immutable release passed the same
363 tests and `compileall` on both Linux hosts. Ubuntu Squid `6.14-0ubuntu0.24.04.4` was installed
with its distribution service masked before package installation, and its package AppArmor profile
remained loaded in enforce mode.

The dedicated `inet codex_egress` table changes only OUTPUT handling. UID 1002 can open new TCP
connections only to `127.0.0.1:3128`; UID 13 can use the local resolved stub and public TCP/443 but
is rejected from private, metadata, site-blocked, non-443, and other destinations. Other local UIDs
cannot connect to the proxy. The proxy listens only on IPv4 loopback. Squid's required coordinator
runs as root with only `CAP_SETUID` and `CAP_SETGID`; its UID-13 worker owns the listener and has an
empty effective capability set. The coordinator had no TCP socket, the package default listener
never appeared, INPUT/FORWARD remained unchanged, and administrator SSH survived every step.

Credential-free probes produced one exact audit record per accepted or denied CONNECT request. An
allowlisted `api.openai.com` TLS tunnel reached the origin and returned HTTP 421 without credentials;
`example.com`, private space, metadata, and the public Control Host address were denied. Direct
Runner TCP/443, DNS, UDP/443, and non-proxy loopback connections failed, while another unprivileged
UID was rejected before Squid. Stopping the proxy failed closed and restarting restored the UID-13
listener. A forced real log rotation preserved the original audit inode as the rotated file,
created `proxy:proxy 0640` replacements below a root-owned non-writable directory, and the next
denied request was recorded exactly once. Every access-log line matched the fixed metadata-only
schema, and no Squid AppArmor denial was recorded.

Only after those probes passed was `/srv/codex-runner/etc/config.json` atomically changed to the
fixed `http://127.0.0.1:3128` endpoint. The real Runner configuration loader accepted it as the
locked `codex-runner` account. Both `s2` and `s3` then switched atomically to immutable release
`e44b66bddeda415267674a0590423187fcc45fe4`; two post-switch read-only preflights returned strict
idle. Restoring the Dispatcher timer produced four observed successful idle sweeps with no selected
Issue, WorkItem, or Turn. A final read-only Fixture snapshot found six open pull requests, no active
Actions run, and unchanged `main` SHA `f5037925502905fd3d22a807df7291ba1004bab9`.

No Fixture GitHub, Slack, branch, pull-request, merge, release, tag, or application-data write was
performed by this infrastructure activation. The temporary `s3` sudoers grant was moved into the root-only rollback
directory at mode `0600`, and a new SSH session proved passwordless sudo unavailable. The Dispatcher
and backup timers, proxy, firewall, AppArmor policy, audit retention, previous releases, original
Runner configuration, and SQLite backup remain independently recoverable. The 14-domain bootstrap
allowlist is version-specific evidence from Codex CLI 0.147.0, not an upstream compatibility
guarantee. Container-visible proxy routing and aggregate per-WorkItem disk isolation remain required
before rootless container activation.

## Runner rootless Docker host bootstrap — 2026-08-21

The dedicated `s3` Runner now has an initialized but not yet admitted rootless Docker engine. Before
the host change, the `s2` Dispatcher timer was disabled, the service was inactive, a new protected
SQLite Online Backup returned `integrity_check=ok`, local status had no active run, and a read-only
`ssh-preflight` returned strict `idle` without external writes. The Runner global lock was
acquirable, no exact Runner/Codex/Docker process existed, and the existing Runner configuration,
package inventory, and nftables state were copied into a checksummed root-only rollback directory.

The official Docker Ubuntu repository key was verified by its full fingerprint before the exact
Ubuntu 24.04 packages were installed. Docker Engine, CLI, and rootless extras are pinned at
`5:29.7.2-1~ubuntu.24.04~noble`; `containerd.io` is pinned at
`2.3.3-1~ubuntu.24.04~noble`. The four packages are held. The root-owned system-level
`docker.service`, `docker.socket`, and `containerd.service` remained masked throughout installation,
stayed inactive afterward, and exposed no rootful socket.

Linger and the user manager were enabled only for the locked `codex-runner` account. Its rootless
`docker.service` is active and enabled on the mode-`0700` runtime directory, with the API socket
owned by that account. The daemon reports Docker 29.7.2, `overlayfs`, cgroup v2, and the `rootless`,
`seccomp`, and `cgroupns` security options. A root-owned drop-in fixes `slirp4netns` and explicitly
permits the RootlessKit host-loopback path required by the future container-visible proxy endpoint;
the protected empty Docker CLI configuration prevents inherited user contexts or credentials from
selecting another daemon.

No image was pulled or built, no container was created, and no non-default Docker network exists.
The Runner remained on immutable release `e44b66bddeda415267674a0590423187fcc45fe4` with the legacy
direct execution mode and the existing audited loopback proxy. The new offline rootless execution
implementation at commit `ec34b19` passed all 371 unit tests, `compileall`, and `git diff --check`,
but was not deployed and was not enabled in production.

After initialization, the Runner lock was still idle and the rootless daemon still had zero images
and zero containers. The existing proxy and nftables services remained active, a normal timer sweep
exited successfully with no active run, and a second read-only `ssh-preflight` again returned
`idle`, `external_writes=false`, and `authorizes_apply=false`. The Dispatcher timer was restored.
No GitHub, Slack, WorkItem, Turn, branch, pull request, Action, merge, deployment, release, or tag
write was performed by this bootstrap. The temporary `s3` sudoers grant was moved into the
root-only rollback directory at mode `0600`; an independent SSH connection then proved that
passwordless sudo was no longer available while the rootless daemon remained active.

This checkpoint proves only package provenance, rootless daemon ownership, rootful exclusion, and
safe coexistence with the direct Runner. It does not admit container Turns. The next separately
authorized gates are an aggregate disk-limit/FUSE proof, immutable image acquisition and digest
verification, dedicated proxy-only Docker networking, and one disposable container boundary test
before any Runner configuration switch. Rollback is to freeze the Dispatcher timer, stop and
disable the user `docker.service`, disable linger, and retain the masked rootful units and persistent
Runner state for reconciliation.

## Runner rootless Docker admission checkpoint — 2026-08-21

Before admission work, the `s2` Dispatcher timer was disabled and its service was inactive. A fresh
protected SQLite Online Backup was created and returned `integrity_check=ok`; read-only preflight was
strictly idle. Checksummed root-only rollback directories on both hosts preserve the prior releases,
configuration, units, firewall state, package inventory, and Docker bootstrap artifacts.

The Runner pulled only the reviewed official `linux/amd64` image
`ghcr.io/openai/codex-universal@sha256:1641c7bc30b00e0c5d4858b3e4da750123e9802fdb8086e9baa5afa2bc99393c`.
Temporary registry bootstrap entries and the pull-only daemon proxy were removed afterward, restoring
the original 14-domain audited allowlist. The retained image consumes approximately 43.69 GB and is
not safely reclaimable. `br_netfilter` is loaded persistently, bridge netfilter is enabled, and the
pre-existing audited nftables table remained byte-for-byte unchanged. The dedicated local bridge
`codex-egress` is fixed to `172.30.0.0/24` with gateway `172.30.0.1`, masquerading enabled,
inter-container communication disabled, IPv6/internal/attachable/ingress disabled, and no residual
container attachment.

Credential-free container probes proved the read-only root filesystem; exact 1 GiB `nosuid,nodev`
`/tmp`; zero effective capabilities; `NoNewPrivs`; seccomp; 8 GiB memory, zero extra swap, 512 PIDs,
and two CPUs; no Docker socket; proxy-only public access; direct, metadata, private, and unrelated
public denial; proxy-stop fail-closed behavior; inter-container denial; exact cleanup; and the
read-only digest-verified Codex CLI mount. No credential or Prompt entered these probes.

An exact FUSE A/B probe found that default `mkfs.ext4` deallocated most blocks from the preallocated
regular backing file. Commit `817be49` added the fixed `-E nodiscard` argument. The corrected 64 MiB
live image remained dense before and after ENOSPC, delete, unmount, remount, and clean `e2fsck`, with
its marker preserved and exact temporary cleanup. All 382 then-current tests passed locally and on
both Linux hosts.

The initial rootless socket used a subordinate mapped Docker group and was correctly rejected by the
Runner ownership boundary. A root-owned protected XDG Docker configuration now fixes daemon group
`root`, which maps to the locked account's host GID; the socket is `1002:1002`, mode `1660`, with no
other permissions. Commit `7f611ee` also made error-only blocked Turns publish one bounded generic
Runner error code to Slack without result fields or raw output. All 383 tests plus `compileall` passed
locally and on both Linux hosts before both immutable releases were switched to that commit.

Private Fixture Issue #20 then exercised the remaining fail-closed path. Exactly one WorkItem
`wi_70b009360e9de76a19ee8eb4` and one Turn `turn_dc638a73cd6247b2bf27f357a8351309`
were persisted. The pre-fix socket check blocked before Codex execution, leaving a durable finished
Runner record with bounded `docker_boundary_invalid`, no agent result, session, published SHA, or PR.
The root and failure Slack deliveries were each recorded once. After the operational socket fix, the
only recovery action was `sync_tracker_state`; it did not call the Runner, START/RESUME, or resend the
Prompt. Repeated preflight remained idle; the Fixture `main` SHA stayed
`f5037925502905fd3d22a807df7291ba1004bab9`, open PR count stayed six, and active Actions stayed zero.
The failed WorkItem image, mount, Turn, branch state, Issue, and delivery evidence remain preserved.

A successful Codex container Turn is still not admitted. The 80 GiB root filesystem had
23,663,915,008 available bytes while a new 8 GiB image plus the fixed 16 GiB reserve requires
25,769,803,776 bytes, an exact shortfall of 2,105,888,768 bytes. Safe cache/log cleanup cannot close
that gap, the reviewed Docker image cannot be pruned, and the failed WorkItem evidence cannot be
deleted. The Dispatcher timer therefore remains disabled and temporary administrative access remains
in place pending an explicit capacity decision. No secret, Prompt, private key, complete Runner
output, merge, deployment, release, tag, or push was produced by this checkpoint.

## Runner companion recovery and successful container fixture — 2026-08-21

This later checkpoint supersedes the admission shortage above for the dedicated private Fixture.
The broad `codex-universal` image was replaced by the reviewed Python/npm Web baseline, and the
active Runner image was pinned to
`ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:da3662343e86ebeeba97f54f1c7f03faf03b988e07677cbd171a80d9903c772d`.
Private Fixture Issue [`#24`](https://github.com/longwdl/codex-dispatcher-fixture/issues/24)
then created exactly one 8 GiB WorkItem image and reached Codex, but Turn 1 deterministically blocked
before repository changes because the container mounted `/usr/local/bin/codex` without its required
`/usr/local/bin/codex-code-mode-host` sibling. The blocked checkpoint preserved one WorkItem, one
session, one Turn, the original branch and directory, one Slack root plus one failure reply, no
published SHA, and no PR.

Commit `df1654b280011e0a0696f598af66c4c0d8f195fd` added an independently configured and freshly
verified companion digest, a sixth read-only Turn mount, and a backward-compatible protected
`codex-session-tools.json` sidecar. The original version-1 `codex-session.json` is never rewritten,
so the previous Runner can still read it after restoring the previous configuration. All 390 tests,
`compileall`, and `git diff --check` passed locally. The exact root-owned candidate passed the same
390 tests and `compileall` on `s3` under the production `codex-runner` account and `umask 077`.

Before switching, the Dispatcher timer was disabled, its service was inactive, SQLite had no active
Turn and passed `integrity_check`, read-only preflight was strictly idle, the Runner lock was
acquirable, and Docker had no container. One mode-`0600` Online Backup was created before the Runner
switch and another immediately before the GitHub writes. The root-owned Runner `current` symlink moved from release
`d7753fbbe2bae2ea3a16fa08c6114ad0b6c91ba8` to the exact commit above, and the protected config
added only companion SHA-256
`00ecf5d040865b97884c488883abd342581c2a432debe7a54e4646bceee3d2d6`. The prior config remains at
`/srv/codex-runner/etc/config.json.pre-code-mode-host-d7753fb-to-df1654b`. No image rebuild or pull,
sshd, firewall, proxy, network, Docker daemon, or Control Host release change was required.

A credential-free `--network=none`, read-only-rootfs probe mounted only the two executables, checked
both hashes, executed `codex --version` and the companion help path, removed its container, and left
no Docker state. A configuration-derived STATUS then returned the original Turn 1 as the same
`finished/blocked` record with session `01a0255f-2cec-79e3-a892-3fab14e584db` and unchanged base
SHA `6bea603ab3eb0fc29f678fe069273565b602bc57`.

One marker-bounded maintainer comment re-enabled Issue `#24`. Read-only planning selected exactly
that Issue, and an additional pure resolution proof required `action=reactivate`, `is_new=false`,
the original WorkItem `wi_59089b353ecda298b262a063`, branch
`codex/issue-24-59089b353ecd`, directory, session, and next Turn number `2`. The only write-enabled
sweep created Turn `turn_5f3e0ff91d694653b02a21ee937bfcb5` and used RESUME. It did not PREPARE,
START a replacement session, or create a second WorkItem.

Turn 2 finished `completed` at checkpoint `acb03e63f045ec5642d9e19fe44b659b53834284`.
Independent read-back proved:

- SQLite remained `integrity=ok`, contained exactly the original WorkItem and two ordered Turns for
  Issue `#24`, bound Draft PR
  [`#25`](https://github.com/longwdl/codex-dispatcher-fixture/pull/25), and had no active Turn;
- the original session file remained byte-identical at SHA-256
  `a30e7387694a0f5d37bf0cad41b26480984369c4311a2efb68ad156f9097cee5`; the new mode-`0600`
  sidecar bound the same WorkItem, session, image, primary Codex hash, and companion hash;
- strict Runner STATUS returned Turn 2 as `finished/completed` with the same session and checkpoint,
  while the worktree retained the same branch, exactly one new commit, and only `README.md` changed;
- Issue `#24` had `agent:review`, exactly the original fixed status comment plus the maintainer
  context, and exactly one open Draft PR with base `main`, the deterministic head branch, one commit,
  and a one-file README-only diff;
- GitHub Actions run
  [`32512672458`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32512672458)
  completed successfully on attempt 1 for the exact PR head;
- Slack contained exactly three messages in the original thread: one root, the retained Turn 1
  failure, and one Turn 2 result. The root was not duplicated.

An immediate read-only preflight was idle, one manually observed repeated sweep returned idle, and a
final preflight was again idle with an unchanged database. Final backup
`state-20260821T182235.215407Z.db` is mode `0600`, passed `integrity_check`, and contains the review
WorkItem, PR binding, blocked Turn 1, and completed Turn 2. The Dispatcher timer remains disabled;
PR `#25` remains unmerged. No credential, Prompt, private key, full Runner output, merge, deployment,
release, tag, force-push, branch deletion, sshd change, or network-policy change was produced.

## Slack Web API exact-retry fixture — 2026-08-20

This fixture proves the provider-side contract required before enabling the real outbound Slack
publisher. It targeted only Workspace `T0BQ60N9WH4` and private channel `C0BR2D0MS8Y` with the
installed outbound-only Slack App. Before the write, `auth.test` matched the exact Workspace and a
bot identity, the token reported `chat:write`, the protected Dispatcher configuration parsed, and a
read-only SSH preflight returned `idle` with `external_writes=false`.

The source-tree-only `slack-idempotency-fixture` entry required `--apply`, exact Workspace/channel
arguments, canonical UUIDv4 fixture `38667953-3dfa-4ee8-8c95-16c613ae450d`, and the ephemeral
`CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1` gate. It sent the exact same root report twice with
stable client message ID `c22ecb14-02ae-5cb0-8078-2fbba4d87a6b`, markup, mention expansion, link
unfurling, and proxy inheritance disabled. The requests were spaced to respect the per-channel
posting limit and had no automatic retry.

Both calls returned channel `C0BR2D0MS8Y`, message timestamp `1787213067.081109`, and the same
[permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787213067081109).
The maintainer independently inspected the private channel and confirmed that the Fixture ID was
visible in exactly one message. The fixture write gate was absent in a new login shell afterward.

Immediately before the fixture, all 308 offline tests passed; `compileall`, CLI help, and
`git diff --check` also passed. The fixture did not open or mutate SQLite and did not access GitHub,
the Runner, Actions, branches, pull requests, Issues, merges, releases, or deployment facilities.
It proves the real `chat.postMessage` exact-retry receipt contract for this App/Workspace/channel;
it does not replace a later end-to-end WorkItem/Turn/Slack-thread acceptance test.

## Slack outbound normal end-to-end fixture — 2026-08-20

This fixture exercised the optional real Slack publisher through the normal recovery-first
`ssh-run-once` entry point rather than a fault hook. The maintainer created and separately moved
private Fixture Issue
[`#16`](https://github.com/longwdl/codex-dispatcher-fixture/issues/16) from `agent:paused` to
`agent:ready`. Its strict task specification allowed only one existing README fixture value to
change from `p1-ssh-transport-01` to `p1-slack-e2e-01`; Codex was forbidden to push, open a PR,
merge, deploy, release, delete a branch, or modify another file.

Before the write, SQLite passed `integrity_check`, no WorkItem or Turn was active, protected config
with `[slack_runtime]` parsed, and read-only preflight uniquely selected Issue `#16` as
`ready_candidate`. The first normal invocation failed closed at `trusted mirror Git stage failed:
base_fetch` before claim: the Issue remained ready with no comment, and SQLite contained no Issue
`#16` WorkItem or Turn. Direct GitHub connectivity then recovered, a second mode-`0600` online
backup passed `integrity_check`, and preflight again uniquely selected the same Issue. The accepted
retry used the normal SSH and Slack write opt-ins and returned `review`.

The follow-up fix retries only this fixed read-only `base_fetch` once, with both attempts sharing
the original 120-second deadline. It does not retry mirror/config/SHA validation, does not classify
or expose provider stderr, and leaves persistent failures generic. Offline tests prove transient
success, exactly two persistent-failure attempts, and no second attempt after budget exhaustion;
the existing pre-claim ordering still proves no Issue, SQLite, Runner, or Prompt write can precede
the fetch.

After the fix was committed, the real protected refresher fetched only
`longwdl/codex-dispatcher-fixture` `main` and resolved exact current SHA
`f5037925502905fd3d22a807df7291ba1004bab9`; it performed no remote write. This confirms the live
credential, fixed argv, protected mirror, and SHA-verification path, but deliberately does not
manufacture a network outage. The original provider-level failure subtype remains unknown because
raw Git stderr is intentionally neither persisted nor exposed.

### Stable identities and receipts

- WorkItem: `wi_a6e9f94abfd96a11e9e70ec6`.
- Turn: `turn_2dbdc421755144498012f0cf73947d01`, number `1`, finished/completed.
- Codex session: `01a01e6d-5836-7be3-8d41-817d78370746`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-16`.
- Base SHA: `790c3e0b361f727863e3e6d86ee6e2dce16b4faf`.
- Published/output SHA: `77504f36c59c2448a6704cdf0e80c3bd099f3345`.
- Task branch: `codex/issue-16-a6e9f94abfd9`.
- Slack [root](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787216871827959),
  timestamp `1787216871.827959`.
- Slack [result reply](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787216946860319?thread_ts=1787216871.827959&cid=C0BR2D0MS8Y),
  timestamp `1787216946.860319`.
- Draft PR
  [`#17`](https://github.com/longwdl/codex-dispatcher-fixture/pull/17).
- GitHub Actions run
  [`32352400905`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32352400905).

Independent Runner STATUS returned `finished` with the same WorkItem, Turn, session, output SHA,
completed result, and only `README.md` in changed paths. SQLite contained exactly one WorkItem, one
Turn, and two delivered Slack outbox records. GitHub contained one fixed Issue status comment with
the Slack root link and one open CLEAN Draft PR. The PR had one commit and changed only `README.md`,
one insertion and one deletion. The task branch, PR head, SQLite publication SHA, Runner head, and
successful Actions head were identical; `main` remained at the base SHA.

After the result reply existed, a fresh `chat.getPermalink` for the root appended
`thread_ts=1787216871.827959&cid=C0BR2D0MS8Y` to the original bare root URL. The channel, path, and
message timestamp were unchanged, the persisted bare root URL remained valid, and the result reply
permalink remained byte-for-byte identical. The runtime intentionally preserves the original
receipt rather than rewriting durable state for this provider presentation change.

Three attempt/idle-boundary SQLite online backups were retained; each was owned by the Dispatcher
user, mode `0600`, and passed `integrity_check`. Final preflight returned `idle`. An immediate normal
write-enabled sweep also returned `idle`; post-checks still found one WorkItem, one Turn, two Slack
deliveries, one comment, one Draft PR, and one successful Actions run. No second session, branch,
message delivery, PR, or workflow run was created. No merge, default-branch write, deployment,
release, tag, branch deletion, or production access occurred.

## Slack root/result receipt-loss recovery fixture — 2026-08-20

This fixture exercised both real Slack receipt-loss windows through the normal Dispatcher outbox,
using only private Fixture Issue
[`#18`](https://github.com/longwdl/codex-dispatcher-fixture/issues/18) and the already proven
Workspace/channel contract. Before each fault, `auth.test` matched Workspace `T0BQ60N9WH4` and an
installed bot; protected config, exact tool pins, SQLite integrity, a read-only preflight, the SSH
write gate, the Slack write gate, and the repository-valued fault gate all passed. The source-tree
fault path was committed separately before the live writes.

The root stage accepted only a new `ready_candidate`. After the Runner had durably prepared the
WorkItem but before any Turn existed, Slack returned root timestamp `1787223231.513909` and its
[permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787223231513909).
The fixture discarded that complete receipt. SQLite then contained WorkItem
`wi_887b852ac5834765ac2571a7` in `ready`, no Turn or Codex session, no branch checkpoint or PR, no
Slack thread binding, and exactly one `prepared` root outbox record. The Issue was
`agent:dispatching`; read-only preflight selected only `start_claimed_turn`. The verified online
backup `state.pre-slack-root-receipt-i11tk0y3.db` was mode `0600` and passed `integrity_check`.

The terminal stage was admitted only from that exact root-recovery state. The same root request
returned the original timestamp/permalink and was atomically bound before execution. The one Turn
then finished, the exact checkpoint was published, Draft PR
[`#19`](https://github.com/longwdl/codex-dispatcher-fixture/pull/19) was bound, and the fixed Issue
comment was updated. Slack returned result timestamp `1787223348.057679` and its
[reply permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787223348057679?thread_ts=1787223231.513909&cid=C0BR2D0MS8Y);
the fixture discarded only that complete receipt. SQLite retained the WorkItem in `review`, one
finished/completed Turn, the root as `delivered`, and only the result as `prepared`; the Issue stayed
`agent:dispatching`. Read-only preflight selected only `sync_tracker_state`. The second verified
online backup `state.pre-slack-terminal-receipt-3fmok59u.db` was mode `0600` and passed
`integrity_check`.

One ordinary `ssh-run-once` retried the same result outbox identity and returned
`state_synchronized`. Its durable timestamp and permalink were byte-for-byte equal to the discarded
receipt, and the Issue moved to `agent:review`. Final identities were:

- one WorkItem `wi_887b852ac5834765ac2571a7`;
- one Turn `turn_347bda0de02442899af7f203d5c117d2` and Codex session
  `01a01ecf-60b4-7121-8ca4-ceeb62fb6d4e`;
- base/main SHA `790c3e0b361f727863e3e6d86ee6e2dce16b4faf` and checkpoint
  `7a90afd4dbbec378dbfb7cad45bfa3c72452e8a2`;
- one branch `codex/issue-18-887b852ac583`, one Draft PR `#19`, one fixed Issue comment, and two
  delivered Slack records in the original thread;
- one successful exact-head Actions run
  [`32361317694`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32361317694).

Independent Runner `STATUS` returned `finished` for the same WorkItem, Turn, and session with no
artifact. Fixture `main` remained at the base SHA. A final read-only preflight and an immediate
write-enabled sweep both returned `idle`; counts and receipts were unchanged. The Dispatcher Slack
App still has no history/search or inbound scope. Exact-retry equality plus the separately
human-confirmed provider idempotency contract proves recovery without a second delivery; visual
inspection of this private thread remains a maintainer-only cross-check. No merge, deployment,
release, branch deletion, tag, Issue close, or production access occurred.

## GitHub completion comment/label receipt-loss recovery fixture — 2026-08-20

This fixture completed the lifecycle of the same private Issue
[`#18`](https://github.com/longwdl/codex-dispatcher-fixture/issues/18). After explicit authorization
to operate the disposable Fixture repository, the operator re-read the one-file README diff,
confirmed Actions run
[`32361317694`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32361317694)
was successful for exact head `7a90afd4dbbec378dbfb7cad45bfa3c72452e8a2`, marked Draft PR
[`#19`](https://github.com/longwdl/codex-dispatcher-fixture/pull/19) ready, and merged it using that
SHA as an optimistic-concurrency guard. This was an authorized operator action outside the
Dispatcher; the Dispatcher still has no merge operation. Fixture `main` advanced to merge commit
`f5037925502905fd3d22a807df7291ba1004bab9`, while the task branch remained at the checkpoint.

The live-only mechanism was committed before the writes and all 317 offline tests, `compileall`,
`git diff --check`, and the staged sensitive-pattern check passed. Read-only preflight then selected
`complete_merged_work_item` for only Issue `#18`, WorkItem
`wi_887b852ac5834765ac2571a7`, and PR `#19`.

The first live attempt failed closed before any target write because recovery planning legitimately
audited an older completed WorkItem first. Its online backup was valid, but
`fault_triggered=false` and the target stayed in review. The fixture wrapper was narrowed so
non-target historical PR reads cannot arm a fault, while every non-target write remains rejected;
the focused and full 317-test suites passed again and the correction was committed separately.

The accepted comment stage first persisted the irreversible local `completed` tombstone, then
updated only the exact fixed comment and discarded its successful response. It left the Issue at
`agent:review`, with no active Turn and no Source, Runner, Git Publisher, or Slack Publisher call.
Backup `state.pre-completion-comment-receipt-kdn_qmp5.db` was mode `0600` and passed
`integrity_check`; preflight then selected only `sync_tracker_state`.

The label stage idempotently updated the same comment, applied `agent:completed`, read the exact
Issue back from GitHub, and only then discarded that verified response. Backup
`state.pre-completion-label-receipt-20gig5xq.db` was also mode `0600` with successful integrity.
The one fixed comment was updated at `2026-08-20T11:50:50Z`; the completed-label timeline event was
later, at `11:53:25Z`. The Issue remained open with exactly one dispatcher comment and retained its
Slack execution link.

Independent final reads proved one completed WorkItem, one finished/completed Turn, the original
session, Runner directory, branch, checkpoint, merged PR, successful Actions run, and two delivered
Slack records. Runner `STATUS` returned `finished` for the same WorkItem, Turn, and session with no
artifact. Final preflight and two ordinary write-enabled sweeps were all `idle`; no new Turn,
session, Prompt, branch, PR, comment, workflow run, Slack delivery, deployment, release, tag, branch
deletion, or Issue close occurred.

## SSH CLI human-merge completion projection fixture — 2026-08-19

This fixture completes the lifecycle of Fixture Issue
[`#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12) after its START-receipt
recovery. The dispatcher did not merge the pull request. The maintainer independently reviewed the
README-only diff, confirmed Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603) was
successful at exact checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078`, marked Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) ready, and explicitly merged it.

Before the completion sweep, read-only preflight selected `complete_merged_work_item` for the exact
Issue, WorkItem `wi_594a1305a087ff78a0ab32f8`, and PR `#13`. The normal double-opt-in sweep then:

- required repository `longwdl/codex-dispatcher-fixture`, base `main`, task branch
  `codex/issue-12-594a1305a087`, and PR head SHA to match the persisted binding;
- committed the irreversible local WorkItem state `completed` before projecting GitHub state;
- updated the one fixed status comment to `agent:completed` at `2026-08-19T01:47:34Z`;
- applied the `agent:completed` label at `2026-08-19T01:47:37Z`, after the comment update;
- left the Issue open and retained the task branch.

Independent read-back found exactly one WorkItem and one finished/completed Turn. SQLite retained
PR `#13` and the exact checkpoint. The task branch still resolved to that checkpoint, while `main`
resolved to merge commit `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. No second workflow run,
Turn, session, branch, or PR appeared. A subsequent read-only preflight and a repeated write-enabled
sweep both returned idle.

This proves the normal AC-055 completion path and ordered projection. It does not prove lost-receipt
recovery for the completion comment or label, automatic merging, Issue closing, branch deletion, or
deployment; those actions remain forbidden or separately covered only by offline fault tests.

## SSH CLI exact Dispatcher process-kill recovery fixture — 2026-08-19

This fixture covers the post-claim/pre-persistence operating-system boundary with a real
`SIGKILL`. It does not select a process by name. A dedicated parent starts one exact child argv in a
new session, waits for an identity-bound private-pipe handshake emitted only after GitHub claim, and
kills only that verified still-running child.

### Kill boundary

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#10`](https://github.com/longwdl/codex-dispatcher-fixture/issues/10),
  node ID `I_kwDOT3NfX88AAAABNQOefw`.
- The successful process fixture returned `child_exit_code=-9`, `termination_signal=SIGKILL`,
  `local_work_item_persisted=false`, and `runner_reached=false`.
- SQLite passed `integrity_check`, contained no WorkItem for Issue `#10` and no active Turn, while
  the Issue was exactly `agent:dispatching`.
- Read-only preflight selected `ready_recovery/recover_orphan_claim` for that exact Issue.
- The accepted pre-kill online backup
  `state.pre-claim-acquired-process-kill-0ukms_yw.db` was mode `0600` and passed
  `integrity_check`.

Two earlier pre-claim attempts encountered the then-active 120-second GitHub Git transport timeout.
The first exposed that a shorter parent handshake timeout could terminate Python while its isolated
Git process group was still running; the exact group was stopped, the parent deadline was raised
above the longest bounded pre-claim Git operation, and a second timeout verified clean teardown.
Neither attempt reached claim or changed Issue `#10`. For the successful kill injection only, the
fixture required GitHub REST `main` and the protected cached mirror ref to equal exact SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; this did not alter normal dispatcher fetch behavior.

### Ordinary recovery

When Git HTTPS recovered, one normal double-opt-in `ssh-run-once` consumed the orphan claim without
manual label repair:

- WorkItem: `wi_56cfb4bd6efc095beabb0852`;
- Turn: `turn_44612d44d99b4ac89947b19afd19655f`, number `1`;
- Codex session: `01a01605-beca-7040-adb8-ca3a6e6bd06c`;
- task branch: `codex/issue-10-56cfb4bd6efc`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-10`;
- exact checkpoint: `a17ae709a111cd84d7a08050afa975351190fa73`;
- Draft PR [`#11`](https://github.com/longwdl/codex-dispatcher-fixture/pull/11).

SQLite contained exactly one WorkItem and one finished/completed Turn for the Issue. The PR changed
only `README.md`, one insertion and one deletion, to exact marker `p1-process-kill-v1`; its head and
SQLite publication SHA matched. The fixed Issue comment existed once and the Issue entered
`agent:review`. GitHub Actions run
[`32168464039`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32168464039)
completed successfully for that exact branch/SHA. `main` remained at the base SHA, final preflight
was idle, and an immediate second write-enabled sweep also returned idle.

## SSH CLI START receipt STATUS-only recovery fixture — 2026-08-19

This fixture covers AC-036 with two guarded source-tree stages. The first stage delegates a real
START and discards its response only after strict parsing proves the same WorkItem/Turn identity and
a remote state of `running` or `finished`. The second stage rejects PREPARE, START, and RESUME before
delegation and records the exact recovery operation order.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12),
  node ID `I_kwDOT3NfX88AAAABNQlrSw`.
- WorkItem: `wi_594a1305a087ff78a0ab32f8`.
- Turn: `turn_be25135c133b47c6a580ee585d71cd18`, number `1`.
- Task branch: `codex/issue-12-594a1305a087`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-12`.

Immediately after `start-receipt`, the command reported `fault_triggered=true`,
`recovery_required=true`, and `runner_active`. Independent SQLite read-back found exactly one
running WorkItem and one `reconciling` Turn; local `codex_session_id`, `last_published_sha`,
`pr_number`, result status, and output SHA were all unset. The Issue was `agent:running`, and
preflight selected only `reconcile_active_turn` for the exact WorkItem/Turn.

The `start-status-recovery` command then reported `recovery_guarded=true`,
`fault_triggered=false`, `runner_operations=["status","export"]`, and `review`. It reused the same
WorkItem and Turn, bound Codex session `01a0160b-4cb9-7522-9b32-41dff7ab73b6`, and published exact
checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078` to the existing task identity. Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) changed only `README.md`, one
insertion and one deletion, to exact marker `p1-start-status-v1`. The fixed Issue comment existed
once, the Issue entered `agent:review`, and GitHub Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603)
completed successfully for the same branch/SHA.

Both stage backups were mode `0600` and passed `integrity_check`. Final SQLite contained one
WorkItem and one finished/completed Turn, `main` remained at
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, final preflight was idle, and an immediate normal
write-enabled sweep also returned idle. A brief Issue-list visibility delay after creation was
resolved by an explicit maintainer ready-label transition; no fault write ran until the list read and
preflight both selected the exact Issue. This proves STATUS-only protocol recovery and does not claim
a physical network-link or SSH-daemon interruption.

## SSH CLI exact transport-process interruption fixture — 2026-08-19

This fixture covers AC-021 and AC-056 with a real OpenSSH client-process `SIGKILL`. It did not
change the SSH daemon, firewall, routes, host networking, or any unrelated connection. The guarded
entry received only the exact primary child capability; it had no process-name lookup or bulk-kill
path.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#14`](https://github.com/longwdl/codex-dispatcher-fixture/issues/14),
  node ID `I_kwDOT3NfX88AAAABNUW04w`.
- WorkItem: `wi_b7edba3be957aa3d4a851c56`.
- Turn: `turn_7e8f8db322764d579c51595e4b2725ab`, number `1`.
- Task branch: `codex/issue-14-b7edba3be957`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-14`.
- Pre-live implementation commit: `3221884`.

Before the live write, all 286 offline tests, `compileall`, and `git diff --check` passed. Read-only
preflight selected only Issue `#14`; SQLite had no binding for it, the derived branch and PR did not
exist, and `main` was `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. The independently created
pre-live online backup and both command-created backups were mode `0600` and passed
`integrity_check`.

After the local WorkItem and Turn were durable, a separate hook-free SSH transport issued only
STATUS. Attempt 2 observed the Runner's durable executing signature
`state=unknown,error_code=turn_outcome_unresolved`; the local WorkItem was still running and the
same Turn was still starting. The hook freshly verified immutable argv and exact
`PID=PGID=SID=80465`, then sent `SIGKILL` only to that process group. Independent read-back found the
PID gone, SQLite integrity `ok`, one running WorkItem, and the same Turn in `reconciling` with no
local session, output SHA, publication SHA, or PR. Read-only preflight selected exactly
`reconcile_active_turn`.

The guarded recovery rejected PREPARE, START, and RESUME before delegation and reported exact
`runner_operations=["status","export"]`. It reused the same WorkItem and Turn, bound the existing
Runner session (local and remote SHA-256 fingerprint prefix `0926e3c0ccb6901c`), and published exact
checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`. Draft PR
[`#15`](https://github.com/longwdl/codex-dispatcher-fixture/pull/15) changed only the allowed Fixture
path. The one fixed Issue comment existed, the Issue entered `agent:review`, and the one GitHub
Actions run
[`32213983342`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32213983342)
completed successfully at that exact SHA.

Final SQLite contained exactly one WorkItem and one finished/completed Turn; direct Runner STATUS
returned the same Turn, session fingerprint, checkpoint, and completed result. The task branch,
SQLite publication record, PR head, and Actions head all matched. Before human merge, `main` did not
move. Two ordinary write-enabled sweeps and the read-only preflight all returned idle, so no second
Turn, session, branch, PR, comment, or workflow run was created.

### Human merge and completed projection

After explicit review confirmed the README-only diff, exact head SHA, and successful Actions run,
PR `#15` was marked ready and merged without deleting its task branch. GitHub created merge commit
`790c3e0b361f727863e3e6d86ee6e2dce16b4faf`; the PR closed as merged while the task branch remained
at checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`.

Read-only preflight then selected `complete_merged_work_item` for the exact Issue, WorkItem, and PR.
One normal double-opt-in sweep first committed the local `completed` tombstone, updated the single
fixed status comment at `2026-08-19T06:32:21Z`, and applied `agent:completed` at
`2026-08-19T06:32:24Z`. The Issue stayed open. SQLite retained the original single finished Turn,
session, PR, and published SHA; no Runner or Publisher work was introduced. A subsequent normal
write-enabled sweep and read-only preflight were idle.

## SSH CLI same-Issue needs-input resume fixture — 2026-08-19

This section records the first end-to-end Issue lifecycle with two Turns in one persistent Codex
session. It proves the GitHub `needs_input → ready` path in addition to the earlier direct Runner
resume fixture. Slack remained disabled, and no merge or completion transition was performed.

### Turn 1: deliberate missing input

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#6`](https://github.com/longwdl/codex-dispatcher-fixture/issues/6).
- WorkItem: `wi_e5c93ce564fa68c4be09cc5c`.
- Task branch identity: `codex/issue-6-e5c93ce564fa`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-6`.
- Codex session: `01a015c4-4053-7812-bed0-d0b39db406be`.

The reviewed Issue intentionally omitted the exact README marker value and required the first Turn
to ask rather than guess. Read-only preflight selected exactly Issue `#6`; the write-enabled sweep
returned `needs_input` for Turn `turn_d550ef3560864379981d58304582f975`. SQLite recorded Turn number
`1`, `result_status=needs_input`, and identical input/output HEAD
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The WorkItem entered `waiting_input`, and the Issue
entered `agent:needs_input`. Independent read-back found no task ref, checkpoint, or PR; `main` did
not move.

### Maintainer context and Turn 2

The maintainer added one reviewed comment:

```text
/codex-context
Use the exact fixture marker value: p1-needs-input-resume-v1
```

Its immutable comment ID was `IC_kwDOT3NfX88AAAABPcWHXQ`. The Issue was then explicitly returned to
`agent:ready`. During this manual transition, the repository's old setup label
`agent:needs-input` was found to conflict with the canonical runtime label `agent:needs_input`, and
the Issue briefly had both `ready` and `needs_input`. Preflight returned idle and no Runner call was
made. The stale hyphenated repository label was unused and removed, the canonical underscore label
was retained, and the missing canonical `agent:completed` label was created. Only after the Issue
had exactly one state label did preflight select it again.

The second write-enabled sweep returned `review` for Turn
`turn_d3879f60603945f8bb1cd605541a3be4`. SQLite recorded Turn number `2`, included only the reviewed
context comment ID, and preserved the original WorkItem, branch, Runner directory, and session. The
checkpoint was `d25edd9d2b3287a596e773c28210f1d9cefa1f05`; Draft PR
[`#7`](https://github.com/longwdl/codex-dispatcher-fixture/pull/7) was the only PR for the task branch
and changed exactly `README.md` by one insertion and one deletion. The resulting marker was exactly
`p1-needs-input-resume-v1`.

GitHub Actions run [`32162425453`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32162425453)
completed successfully for the exact checkpoint and task branch. Final preflight returned idle;
SQLite contained exactly one WorkItem and two ordered Turns for Issue `#6`; `main` remained at its
original SHA. Both pre-Turn SQLite Online Backup API snapshots were mode `0600` and passed
`integrity_check`.

## SSH CLI recorded-publication recovery fixture — 2026-08-19

This fixture covers AC-047's narrow durability window: the exact checkpoint SHA was committed to
SQLite after a successful task-branch publication, but the Turn had not yet advanced from
`checkpointing` to `published`. The source-tree-only fault hook raises at that exact boundary. This
is deterministic process-level exception injection; it does not claim to be an operating-system
process kill or an SSH disconnect.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#8`](https://github.com/longwdl/codex-dispatcher-fixture/issues/8).
- WorkItem: `wi_9eb14638cf6691e8b2a783bb`.
- Turn: `turn_54ec2bff87594f53bb668ae8bf950bc1`.
- Codex session: `01a015d2-3a31-7bd1-b847-b892efdbb795`.
- Task branch: `codex/issue-8-9eb14638cf66`.
- Exact checkpoint: `bd7ac54774d9d098c35f9731b18a34d732094736`.

The `publication-recorded` stage returned `process_interrupted` only after
`record_published_sha()` committed. Independent read-back then found the WorkItem still `running`,
the Turn `checkpointing/completed`, the Issue `agent:dispatching`, no Issue comment, and no PR. The
remote task branch and SQLite `last_published_sha` both resolved to the exact checkpoint. Read-only
preflight selected `resume_publication` for the same WorkItem and Turn.

The `recorded-publication-recovery` stage wrapped both the Runner transport and Publisher in
fail-before-delegate guards. Recovery completed successfully, which proves that neither port was
invoked: it used only the durable checkpoint record, then created and bound Draft PR
[`#9`](https://github.com/longwdl/codex-dispatcher-fixture/pull/9), wrote the fixed Issue status
comment, and projected `agent:review`. SQLite ended with exactly one WorkItem and one finished Turn;
the branch, session, output SHA, and PR binding were unchanged. The README marker was exactly
`p1-recorded-publication-v1`, `main` did not move, and GitHub Actions run
[`32163520437`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32163520437)
completed successfully for the exact checkpoint.

Both accepted stages created a mode `0600` SQLite Online Backup API snapshot and passed
`integrity_check`. A brief GitHub Issue-list visibility delay was observed immediately after the
initial label write; no write was attempted until direct read-back and a later preflight agreed on
the exact ready candidate.

## SSH CLI lost-receipt fixture — 2026-08-19

This section records the bounded, three-stage live fault sequence for Publisher, Draft PR, and
Issue-comment receipts. It used the source-tree-only triple-opt-in fault entry and the normal
Dispatcher recovery path. This run itself does not prove recovery from an actual SSH disconnect or
process kill, and it does not exercise Slack; the later Issue `#10` and `#12` sections above record
the separate process and START-receipt boundaries.

### Admission and initial state

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#4`](https://github.com/longwdl/codex-dispatcher-fixture/issues/4).
- The strict task spec allowed only `README.md`, changing the marker to
  `ssh-lost-receipt-phase-d-v1`.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPw7tg`.
- WorkItem: `wi_80df527531e34d4f039aa16f`.
- Task branch: `codex/issue-4-80df527531e3`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-4`.
- Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; neither the task branch nor
  a matching PR existed.

The first `publisher-receipt` attempt created and verified a private SQLite backup, then stopped at
the trusted-mirror `base_fetch` stage before claim, Runner invocation, or external write. A
restricted read-only retry of the same Git ref succeeded, confirming a transient transport failure
rather than an authentication or repository-state conflict. The exact fault stage was then retried.

### Three discarded receipts

1. `publisher-receipt` started exactly one Codex Turn and discarded the successful new-branch push
   receipt. The command returned `awaiting_publication`; Issue `#4` remained `agent:running`, and
   read-back found the task branch at checkpoint
   `00199ebe3d565048eb6118827aaef9e54ab450cf` with no PR. Preflight then required
   `resume_publication`.
2. `draft-pr-receipt` recovered the same checkpoint, reused the existing remote branch, created
   Draft PR [`#5`](https://github.com/longwdl/codex-dispatcher-fixture/pull/5), and discarded its
   receipt. SQLite had reached `review` with the exact published SHA but had no PR binding; Issue
   `#4` remained `agent:running` and still had no status comment.
3. The following read-only preflight exposed a recovery-order defect: a remote `running` Issue was
   classified as orphaned before its existing terminal WorkItem was considered. No third-stage
   write was attempted while that result was ambiguous. Commit `f42974b` moved the orphan check
   after the persisted binding and terminal-state checks and added a combined Publisher/PR receipt
   regression. All 255 tests passed; live preflight then returned
   `ready_recovery/sync_tracker_state` for the same WorkItem.
4. `issue-comment-receipt` found and bound the existing PR, created the one fixed status comment,
   and discarded that receipt. Preflight again required `sync_tracker_state`. One normal
   double-opt-in `ssh-run-once` returned `state_synchronized`, projected the Issue to
   `agent:review`, and left the subsequent preflight `idle`.

Each accepted stage made an Online Backup API snapshot before entering the sweep. The failed
pre-write Publisher attempt plus the three accepted fault stages left four retained backups; every
file was mode `0600` and passed `PRAGMA integrity_check`. The failed first attempt's backup was
retained rather than silently deleted.

### Independent final read-back

- SQLite passed `integrity_check` and contained exactly one WorkItem and one finished Turn for Issue
  `#4`. WorkItem state was `review`, PR binding was `5`, and both the stored publication SHA and Turn
  output SHA were the exact checkpoint.
- The only Codex session remained `01a015ae-1b9d-7ee3-957a-0a90b21629cd`; no second Turn or session
  was created. Recovery repeated only the exact checkpoint export needed after the ambiguous
  Publisher receipt; it did not PREPARE or START Codex again.
- GitHub contained exactly one open Draft PR for the deterministic branch and exactly one fixed
  Issue status comment. The Issue had `agent:review`, `priority:p1`, and `exec:ssh-cli`.
- The PR changed exactly `README.md`, with one insertion and one deletion; the fixed dispatcher
  marker was present in the only Issue comment.
- The task branch and PR head both resolved to the checkpoint SHA. `main` remained at its original
  SHA; no merge, deployment, release, tag, force-push, or ref deletion occurred.
- GitHub Actions run [`32160041932`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32160041932)
  was `completed/success` for workflow `fixture`, event `pull_request`, run attempt `1`, the exact
  task branch, and the exact checkpoint SHA.

The fine-grained PAT again denied the REST Checks endpoint with HTTP `403`; the permitted Actions
runs endpoint supplied the CI evidence. This run proves the live Publisher lost-receipt read-back
path and the Draft PR and Issue-comment recovery contracts. The narrower post-publication-record,
actual Dispatcher termination, and START-receipt recovery are recorded in the later Fixture
sections above. A physical SSH link/daemon interruption and Slack provider receipt loss remain
separate acceptance work.

## SSH CLI Dispatcher and Publisher fixture — 2026-08-18

This section records the first bounded write-enabled happy-path sweep. It is evidence for the
specific observed run, not authorization for unattended production use or evidence that live crash
recovery and Slack delivery have been proven.

### Admission and immutable identity

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#2`](https://github.com/longwdl/codex-dispatcher-fixture/issues/2).
- Initial labels were `agent:ready`, `exec:ssh-cli`, and `priority:p1`.
- The strict task spec allowed only the README marker value to change to
  `ssh-publisher-phase-d-v1`; Codex was explicitly forbidden to push, create a PR, merge, or deploy.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPZJlw`.
- WorkItem: `wi_70da53f08c0e7d96a08901b8`.
- Task branch: `codex/issue-2-70da53f08c0e`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-2`.

Before the write, `ssh-preflight` selected exactly Issue `#2` as `ready_candidate`, reported no
rejections, and stated `authorizes_apply=false`. The task branch and matching PR did not exist;
Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing SQLite database passed
`integrity_check`, and a mode-`0600` online backup preserved the earlier WorkItem/session/Turn
history before additive migrations or new state were written.

### One write-enabled sweep

The exact entry point required both `--apply` and
`CODEX_DISPATCHER_ENABLE_SSH_WRITES=1`. It returned success for one Turn:

- Turn: `turn_c4e2249059de4b139deff19867c68a27`, number `1`;
- Codex session: `01a01583-2133-7a82-bd40-62278e57bd0d`;
- Runner result: `finished/completed`;
- checkpoint: `071ec769c63b8ab594865611cdc8af46ddd07b7f`;
- resulting WorkItem state: `review`.

The Dispatcher then published only that exact checkpoint to the deterministic task branch, created
Draft PR [`#3`](https://github.com/longwdl/codex-dispatcher-fixture/pull/3), updated the one fixed
Issue status comment, and projected Issue `#2` to `agent:review`. The PR uses base `main`, the exact
task branch as head, and the same checkpoint SHA. SQLite binds PR number `3` and the published SHA
to the same WorkItem and contains exactly one finished Turn for this Issue.

### Independent read-back

- The remote task branch resolved to the checkpoint SHA; `main` remained at its original SHA.
- The PR diff contained only `README.md`, with one insertion and one deletion inside the allowed
  marker; the marker boundaries were unchanged.
- GitHub Actions run [`32155421239`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32155421239)
  was `completed/success` for workflow `fixture`, event `pull_request`, the exact task branch, and
  the exact checkpoint SHA.
- A following read-only `ssh-preflight` returned `idle` with no candidate or recovery action.
- No merge, deployment, release, main-branch update, Slack delivery, or second Codex Turn occurred.

### Second write-enabled sweep

After a new mode-`0600` online SQLite backup passed `integrity_check`, the same double-opt-in
`ssh-run-once` command was executed again. It completed in 3.36 seconds with `status=idle` and null
Issue, WorkItem, and Turn identifiers.

Independent before/after reads proved:

- SQLite remained at two WorkItems and four total Turns;
- Issue `#2` retained the same `review` WorkItem row, PR binding, published SHA, session ID, and
  `updated_at` value;
- it retained exactly one finished Turn with the same ID, number, result, checkpoint, and
  `updated_at` value;
- Issue labels, comment count, and `updated_at` value did not change;
- exactly one Draft PR remained, with the same `updated_at` value and head SHA;
- task-branch and `main` refs did not move;
- the Actions query still returned exactly the original successful workflow run;
- a final read-only `ssh-preflight` again returned `idle`.

This proves the completed happy-path is idle on an immediate repeated sweep. It does not by itself
prove the separate crash/lost-receipt recovery paths.

The fine-grained PAT could list Actions runs but could not read check runs through either the
GraphQL `statusCheckRollup` field or the REST Checks endpoint. CI success is therefore evidenced by
the accessible Actions workflow run, not inferred from those denied check APIs. The separate
controlled Publisher/PR/comment lost-receipt evidence is recorded above.

## SSH CLI Runner fixture — 2026-08-18

This section records the first real SSH CLI protocol run. The Fixture Issue remained open with
`agent:paused`, `exec:cloud`, and `priority:p2`; the test invoked the new protocol directly and did
not claim that end-to-end SSH scheduler label routing exists.

### Release and transport

- Current Runner release: `057226b185dfef70aa1b09bb54561b357f094743`.
- Fixed remote entrypoint: `/srv/codex-runner/bin/codex-runner-v1`.
- Fixed configuration selected Git `/usr/bin/git`, Codex CLI 0.147.0, shared
  `CODEX_HOME=/srv/codex-runner`, and `/srv/codex-runner/work-items`.
- The real `SshRunnerTransport` used the protected host key and identity plus the explicitly
  approved Mac Fixture `assh` proxy shape. An unknown STATUS request was definitively rejected.
- `codex login status` returned `Logged in using ChatGPT`; no API-key billing path was used.
- The release, wrapper, and configuration are user-owned in this Fixture because `ecs-user` has no
  passwordless sudo. Older releases remain available for symlink rollback. This is not the
  production ownership model.

### Migrated 1:1:1 binding and PREPARE

The legacy SQLite anchor, local mirror, GitHub `main`, and the exact remote task ref all agreed on
SHA `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing branch was imported rather than
re-derived:

- WorkItem: `wi_3a97e3d99c30bdcbb50501dd`;
- GitHub Issue: `longwdl/codex-dispatcher-fixture#1`;
- task branch: `codex/issue-1-8e3775879000` with `task_branch_source=migrated`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-1`.

Real PREPARE transferred a 4,804-byte self-contained Git bundle, verified its SHA-256, and created
one clean local repository with no remote. The repository branch and HEAD matched the migrated
binding and base SHA.

### START and resume evidence

All three Turns used the same WorkItem, branch, directory, input HEAD, and Codex session
`01a01486-0eee-7511-85ee-abe46c8bfb8b`:

1. Turn 1 created the session. Its Schema-valid Agent output used `status=blocked` with one question,
   conflicting with the stricter domain rule, so the Runner returned `agent_result_invalid` and no
   checkpoint.
2. Prompt and Schema descriptions were aligned without weakening the parser. Turn 2 resumed the
   exact session and produced a domain-valid final result in the rollout, but the bounded JSONL
   parser returned `codex_output_invalid`. No raw provider output was persisted or forwarded.
3. Static JSONL failure classification was added and deployed. Turn 3 again resumed the exact
   session; it completed with a parsed business result `blocked`, an unchanged output HEAD, and no
   error code. The blocker accurately reported that the isolated Runner cannot prove GitHub
   Timeline and dispatcher dry-run acceptance evidence.

Turn 2's JSONL anomaly did not reproduce in Turn 3, so it remains an observed transient rather
than a confirmed root cause. Future occurrences return a specific bounded JSONL error code.

Post-checks proved the worktree clean, no Git remote, the global lock acquirable, the GitHub task
branch unchanged at the base SHA, `last_published_sha` unset, and no Publisher, PR, Slack, merge, or
deployment action triggered by the task. The
[official Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
confirms that `codex exec --json` emits JSONL, `--output-schema` constrains the final JSON, and an
explicit session ID can be used with `codex exec resume`.

## Historical Codex Cloud fixture — 2026-08-13

Snapshot date: 2026-08-13. This file records non-secret evidence from the dedicated private
fixture. It is not a substitute for repeatable automated tests.

## Repository initialization

- Source repository: `longwdl/codex_cloud_task_scheduler`, private, default branch `main`.
- Fixture repository: `longwdl/codex-dispatcher-fixture`, private, default branch `main`.
- Fixture baseline commit: `b877bdf801afbc1f6edcdca687c8aea8c0532a66`.
- Safe Markdown Issue template commit: `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`.
- Fixture workflow run `31701519356`: completed successfully.
- All 13 `agent:*`, `exec:cloud`, and `priority:*` labels were present.

## Phase 2 admission contract

Fixture Issue `#1` was created with `agent:paused`, `exec:cloud`, and `priority:p2`.

1. With `agent:paused`, live `run-once --dry-run` returned no selected tasks.
2. Maintainer `longwdl` replaced `agent:paused` with `agent:ready`.
3. GitHub Timeline recorded `longwdl` as the Ready-label actor.
4. Live dry-run selected exactly Fixture Issue `#1`.
5. The Issue was restored to `agent:paused`; live dry-run again returned an empty queue.

No branch, pull request, Codex Cloud task, merge, or deployment was created by this test.

## Local task-branch recovery contract

The branch publication protocol is tested against a temporary local bare Git repository. The test
simulates a process failure after the remote branch is created but before SQLite advances the run,
then verifies that retry reuses the exact persisted branch and Base SHA without another write. A
second test advances `main` before recovery and verifies that the persisted Base SHA remains the
task-branch anchor. These tests do not access either configured GitHub repository.

## Phase 3 GitHub write primitive contract

Using the real adapter against Fixture Issue `#1`:

1. The adapter replaced the single state with `agent:dispatching` and verified the reread state.
2. It created one marker-based run comment and then updated the same comment idempotently.
3. It restored the Issue to `agent:paused` and verified the reread state.
4. The temporary test comment was deleted; no matching dispatcher test comment remains.

The test did not invoke Codex Cloud, create a branch, or create a pull request.

## Phase 3 task-branch publication contract

The recoverable branch service ran against Fixture Issue `#1` using deterministic run ID
`fixture-issue-1-branch-contract-v1`. It recorded Base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, created branch
`codex/issue-1-8e3775879000` at that exact commit, then reran and reused the existing branch. The
persisted run ended in `branch_prepared`, `head_sha` equalled `base_sha`, and SQLite
`PRAGMA integrity_check` returned `ok`.

No Issue label/comment, pull request, Codex Cloud task, merge, or deployment was created. The branch
is intentionally retained as the stable input for the later Cloud submission contract test.

## Local Git workspace contract

The hardened Git workspace component cloned the private Fixture through HTTPS into a temporary
local mirror, fetched `main` without tags or recursive submodules, resolved base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, and created local branch
`agent/local-read-only-contract` in an isolated worktree. No branch was pushed and no remote Git
reference changed.

## Accepted fixture risk

GitHub rulesets are unavailable for this private personal-account repository on the current plan.
The owner accepted that residual risk for this credential-free fixture only. This exception does
not apply to a production-connected repository.
