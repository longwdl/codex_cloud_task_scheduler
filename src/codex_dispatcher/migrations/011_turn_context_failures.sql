CREATE TABLE turn_context_failures (
  turn_id TEXT PRIMARY KEY,
  session_generation_id TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  worktree_clean INTEGER NOT NULL CHECK (worktree_clean = 1),
  error_code TEXT NOT NULL CHECK (error_code = 'session_context_failure_clean'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(session_generation_id) REFERENCES session_generations(session_generation_id)
);
