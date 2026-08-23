CREATE TABLE turn_execution_abandonments (
  turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  session_generation_id TEXT NOT NULL REFERENCES session_generations(session_generation_id),
  generation_number INTEGER NOT NULL CHECK (generation_number > 0),
  policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
  session_id TEXT,
  inactive_container_state TEXT NOT NULL
    CHECK (inactive_container_state IN ('absent', 'stopped')),
  inactive_observed_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);

CREATE INDEX turn_execution_abandonments_work_item_idx
  ON turn_execution_abandonments(work_item_id, recorded_at);
