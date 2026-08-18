CREATE TABLE work_items (
  work_item_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number > 0),
  issue_node_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('discovered', 'preparing', 'ready', 'running', 'waiting_input', 'review', 'completed', 'blocked', 'paused')),
  base_branch TEXT NOT NULL,
  task_branch TEXT NOT NULL,
  runner_directory TEXT NOT NULL UNIQUE,
  codex_session_id TEXT UNIQUE,
  slack_channel_id TEXT,
  slack_thread_ts TEXT,
  pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
  base_sha TEXT NOT NULL,
  last_published_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repository, issue_number),
  UNIQUE(repository, task_branch),
  UNIQUE(repository, pr_number),
  CHECK ((slack_channel_id IS NULL) = (slack_thread_ts IS NULL))
);

CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  turn_number INTEGER NOT NULL CHECK (turn_number > 0),
  state TEXT NOT NULL CHECK (state IN ('planned', 'starting', 'running', 'reconciling', 'checkpointing', 'published', 'finished', 'needs_input', 'failed', 'blocked', 'interrupted')),
  issue_revision TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_head_sha TEXT NOT NULL,
  output_sha256 TEXT,
  output_head_sha TEXT,
  result_status TEXT CHECK (result_status IS NULL OR result_status IN ('completed', 'needs_input', 'blocked')),
  result_summary TEXT,
  error_code TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  UNIQUE(work_item_id, turn_number),
  CHECK (
    (output_sha256 IS NULL AND output_head_sha IS NULL AND result_status IS NULL AND result_summary IS NULL)
    OR
    (output_sha256 IS NOT NULL AND output_head_sha IS NOT NULL AND result_status IS NOT NULL AND result_summary IS NOT NULL)
  )
);

CREATE UNIQUE INDEX one_active_turn_globally
ON turns((1))
WHERE state IN ('planned', 'starting', 'running', 'reconciling', 'checkpointing', 'published');

CREATE TABLE work_item_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL,
  turn_id TEXT,
  event_type TEXT NOT NULL,
  event_time TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);
