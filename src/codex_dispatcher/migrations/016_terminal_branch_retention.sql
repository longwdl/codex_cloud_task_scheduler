CREATE TABLE terminal_branch_cleanups (
  work_item_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  branch_name TEXT NOT NULL,
  expected_head_sha TEXT NOT NULL,
  eligible_at TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('prepared', 'completed', 'blocked')),
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('deleted', 'already_absent', 'reconciled_absent')),
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  UNIQUE(repository, branch_name),
  CHECK (length(expected_head_sha) = 40),
  CHECK (length(request_sha256) = 64),
  CHECK (
    (state = 'prepared' AND outcome IS NULL AND error_code IS NULL)
    OR (state = 'completed' AND outcome IS NOT NULL AND error_code IS NULL)
    OR (state = 'blocked' AND outcome IS NULL AND error_code IS NOT NULL)
  )
);

