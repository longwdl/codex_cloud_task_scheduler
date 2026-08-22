CREATE TABLE turn_agent_results (
  turn_id TEXT PRIMARY KEY,
  result_json TEXT NOT NULL,
  result_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);

CREATE TABLE publication_checkpoints (
  turn_id TEXT PRIMARY KEY,
  session_generation_id TEXT NOT NULL,
  previous_sha TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  bundle_sha256 TEXT NOT NULL,
  changed_paths_json TEXT NOT NULL,
  commit_count INTEGER NOT NULL CHECK (commit_count > 0),
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(session_generation_id) REFERENCES session_generations(session_generation_id),
  UNIQUE(session_generation_id, head_sha)
);

CREATE TABLE session_handoffs (
  handoff_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  from_session_generation_id TEXT NOT NULL UNIQUE,
  to_session_generation_id TEXT NOT NULL UNIQUE,
  trusted_facts_json TEXT NOT NULL,
  untrusted_advisory_json TEXT NOT NULL,
  handoff_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  FOREIGN KEY(from_session_generation_id) REFERENCES session_generations(session_generation_id),
  FOREIGN KEY(to_session_generation_id) REFERENCES session_generations(session_generation_id),
  CHECK(from_session_generation_id != to_session_generation_id)
);

CREATE TABLE turn_handoff_bindings (
  turn_id TEXT PRIMARY KEY,
  handoff_id TEXT NOT NULL UNIQUE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(handoff_id) REFERENCES session_handoffs(handoff_id)
);
