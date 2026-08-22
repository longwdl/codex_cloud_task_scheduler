CREATE TABLE turn_completion_gates (
  turn_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'pending', 'unverified')),
  head_sha TEXT NOT NULL,
  task_spec_sha256 TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
);
