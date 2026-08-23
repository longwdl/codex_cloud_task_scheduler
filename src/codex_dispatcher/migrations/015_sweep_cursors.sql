CREATE TABLE sweep_cursors (
  name TEXT PRIMARY KEY CHECK (name IN ('terminal_github_audit')),
  completed_at TEXT NOT NULL
);
