# Owner preparation checklist

This checklist separates work you can prepare now from credentials and remote changes that should
wait until the offline core and contract tests are ready.

## Prepare now

### 0. Create the remote for this source repository

In GitHub, choose the owning account or organization and create a repository named
`codex_cloud_task_scheduler` (or tell the implementer the final name). Prefer **Private** while the
security model and adapters are incomplete.

Create it empty: do not ask GitHub to generate a README, `.gitignore`, or license, because the local
repository already has its initial history. Decide the license before making it public.

After receiving the local commit ID, return only the SSH or HTTPS repository URL. Adding the remote
and pushing are separate remote-state changes and should be explicitly authorized. No PAT is needed
in chat. If the organization supports it, enable secret scanning and push protection.

Do not configure a required CI check until that workflow exists. After the first branch is pushed,
protect `main` against force-push and deletion and require pull requests for subsequent changes.

### 1. Decide the first task target repositories

Prepare, but do not send credentials in chat:

- the `owner/repository` slug for each allowed repository;
- its default branch, normally `main`;
- the GitHub usernames allowed to approve an issue with `agent:ready`;
- repository-relative paths that unattended tasks may modify;
- paths that must always remain manual;
- required PR checks that constitute review evidence.

Start with one low-risk fixture repository and at most one real repository. Do not use a repository
with production deployment credentials for the first test.

### 2. Create a private fixture repository

Create a small private GitHub repository dedicated to contract and smoke tests. It should contain:

- a `README.md` with one clearly marked editable section;
- a minimal test that checks that marker;
- no production secrets, deploy keys, environments, webhooks, or self-hosted runners;
- no automatic deployment workflow.

Suggested name:

```text
codex-dispatcher-fixture
```

The first live task will only change the marked README section and create a draft PR.

### 3. Protect the default branch

On the fixture repository, and later each real repository, create a branch ruleset for `main`:

- require a pull request before merging;
- block force pushes and branch deletion;
- do not allow the future dispatcher identity to bypass the ruleset;
- require the selected CI check before merge;
- keep automatic merge disabled for the initial rollout.

The dispatcher token will have repository-level Contents permission; GitHub cannot reliably reduce
that permission to one branch. The branch ruleset is therefore a required safety control.

### 4. Plan the labels

The implementation will expect the following labels:

```text
agent:ready
agent:dispatching
agent:running
agent:review
agent:needs-input
agent:blocked
agent:paused
agent:discard
exec:cloud
priority:p0
priority:p1
priority:p2
priority:p3
```

You may create them now, or wait for a reviewed bootstrap command. Creating them now is harmless but
is not required for offline development.

### 5. Identify a Codex Cloud test environment

Prepare the intended Cloud Environment ID and confirm that it points to the fixture repository.
Configure it without production credentials. Agent-stage network access should be disabled unless a
specific smoke test requires a small domain allowlist.

Do not run a Cloud task yet. The first real submission should happen only after CLI contract tests
have recorded and validated the installed Codex CLI output schema.

## Prepare later, after the offline core passes

### 6. Choose the GitHub service identity

Preferred order:

1. a dedicated GitHub App installation scoped to allowed repositories;
2. a dedicated machine user with a fine-grained personal access token.

Expected minimum repository permissions:

- Metadata: read;
- Issues: read/write;
- Contents: read/write;
- Pull requests: read/write;
- Checks or Actions: read, if CI status is reconciled.

Do not paste the token into an issue, prompt, repository file, `.env`, or this chat. Store it later as
a systemd credential or an equivalent `0600` service credential.

### 7. Audit PR CI before enabling delivery

The fixture and initial real repositories must run untrusted PR code without valuable credentials:

- use a read-only `GITHUB_TOKEN` unless a job strictly needs more;
- do not expose repository or environment secrets to PR jobs;
- do not use `pull_request_target` to execute the task branch;
- do not use a production-connected self-hosted runner;
- do not mount Docker sockets, SSH agents, kubeconfig, cloud instance roles, or persistent secrets.

### 8. Record the tool versions

The Linux deployment will pin and contract-test:

- Python;
- Git;
- GitHub CLI (`gh`);
- Codex CLI.

The current development snapshot has `codex-cli 0.147.0`, but that is not yet an approved production
pin. Do not automatically upgrade these tools on the dispatcher host.

## Information to return for the next integration phase

No secret values are needed. Return only:

```text
fixture_repository: owner/name
fixture_default_branch: main
fixture_cloud_environment_id: ...
maintainers:
  - github-user
required_checks:
  - check-name
first_real_repository: owner/name  # optional
allowed_paths:
  - ...
additional_denied_paths:
  - ...
```

## Do not do yet

- Do not create a production token for an unfinished adapter.
- Do not expose a local or Linux port to the public internet.
- Do not add production database, SSH, Kubernetes, cloud, or deployment credentials.
- Do not enable automatic merge or deployment.
- Do not connect the dispatcher to an existing production self-hosted runner.
- Do not add `agent:ready` to real work before dry-run and fixture smoke tests pass.
