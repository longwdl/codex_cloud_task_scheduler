# Architecture baseline

## Trust boundaries

```text
Untrusted GitHub issue and comments
               │
               ▼
Task-spec parser and maintainer gate
               │ immutable snapshot + SHA-256
               ▼
Run state machine ───── SQLite audit state
               │
               ▼
Tracker port / Executor port
               │
               ▼
External adapters (deferred)
```

Issue text, comments, repository files, command output, Cloud output, and generated diffs are data,
not instructions for the control host. The control host never executes a verification command copied
from an issue. Such commands can only be requirements for the isolated Cloud task and unprivileged PR
CI.

## Core invariants

- One active run per repository and issue.
- One Cloud task identifier can belong to at most one run.
- Unknown external state is never success.
- A changed issue snapshot cannot receive an older run's result automatically.
- A failed or ambiguous Cloud submission is blocked rather than blindly retried.
- No merge, deployment, production credential, or protected-branch write is part of the core API.

## Dependency policy

The initial runtime uses only the Python standard library. A dependency may be proposed later only
when it materially improves correctness or security and its operational cost is reviewed first.
