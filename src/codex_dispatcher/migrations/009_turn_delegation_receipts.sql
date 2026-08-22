CREATE TABLE turn_delegation_receipts (
  turn_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  observer TEXT NOT NULL CHECK (observer = 'codex_state_db_delta_v1'),
  codex_version TEXT NOT NULL CHECK (codex_version = '0.147.0'),
  root_thread_id TEXT NOT NULL,
  root_model TEXT NOT NULL,
  root_reasoning_effort TEXT NOT NULL,
  agent_count INTEGER NOT NULL CHECK (agent_count >= 0 AND agent_count <= 32),
  receipt_json TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);
