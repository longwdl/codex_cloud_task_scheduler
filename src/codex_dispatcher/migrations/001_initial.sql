CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number > 0),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  state TEXT NOT NULL CHECK (state IN ('discovered', 'claimed', 'branch_prepared', 'dispatching', 'running', 'result_ready', 'applying', 'validating', 'delivering', 'review', 'needs_input', 'blocked', 'discarded')),
  prompt_sha256 TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  base_sha TEXT,
  task_branch TEXT,
  cloud_environment_id TEXT NOT NULL,
  cloud_task_id TEXT UNIQUE,
  cloud_task_url TEXT,
  cloud_diff_sha256 TEXT,
  head_sha TEXT,
  pr_number INTEGER,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_seen_at TEXT,
  last_error_code TEXT,
  last_error_redacted TEXT,
  UNIQUE(repository, issue_number, attempt_no)
);

CREATE UNIQUE INDEX one_active_run_per_issue
ON runs(repository, issue_number)
WHERE state NOT IN ('review', 'needs_input', 'blocked', 'discarded');

CREATE TABLE run_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_time TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
