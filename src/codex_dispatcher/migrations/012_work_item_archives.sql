CREATE TABLE work_item_archives (
  work_item_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('prepared', 'ambiguous', 'archived', 'blocked')),
  expected_head_sha TEXT NOT NULL,
  eligible_at TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_json TEXT,
  response_sha256 TEXT,
  reclaimed_bytes INTEGER CHECK (reclaimed_bytes IS NULL OR reclaimed_bytes >= 0),
  runner_archived_at TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  CHECK (
    (status = 'archived' AND response_json IS NOT NULL AND response_sha256 IS NOT NULL
      AND reclaimed_bytes IS NOT NULL AND runner_archived_at IS NOT NULL AND error_code IS NULL)
    OR
    (status != 'archived' AND response_json IS NULL AND response_sha256 IS NULL
      AND reclaimed_bytes IS NULL AND runner_archived_at IS NULL)
  ),
  CHECK ((status = 'blocked') = (error_code IS NOT NULL))
);
