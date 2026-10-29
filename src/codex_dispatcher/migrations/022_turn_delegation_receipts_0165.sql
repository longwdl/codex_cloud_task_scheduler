CREATE TABLE turn_delegation_receipts_0165 (
  turn_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  observer TEXT NOT NULL CHECK (observer = 'codex_state_db_delta_v1'),
  codex_version TEXT NOT NULL CHECK (codex_version = '0.165.0'),
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

CREATE TRIGGER turn_delegation_receipts_0165_unique_turn
BEFORE INSERT ON turn_delegation_receipts_0165
WHEN EXISTS (
  SELECT 1 FROM turn_delegation_receipts WHERE turn_id = NEW.turn_id
)
BEGIN
  SELECT RAISE(ABORT, 'delegation receipt already exists for Turn');
END;

CREATE TRIGGER turn_delegation_receipts_0147_unique_turn
BEFORE INSERT ON turn_delegation_receipts
WHEN EXISTS (
  SELECT 1 FROM turn_delegation_receipts_0165 WHERE turn_id = NEW.turn_id
)
BEGIN
  SELECT RAISE(ABORT, 'delegation receipt already exists for Turn');
END;

CREATE TRIGGER turn_delegation_receipts_0165_unique_turn_update
BEFORE UPDATE OF turn_id ON turn_delegation_receipts_0165
WHEN EXISTS (
  SELECT 1 FROM turn_delegation_receipts WHERE turn_id = NEW.turn_id
)
BEGIN
  SELECT RAISE(ABORT, 'delegation receipt already exists for Turn');
END;

CREATE TRIGGER turn_delegation_receipts_0147_unique_turn_update
BEFORE UPDATE OF turn_id ON turn_delegation_receipts
WHEN EXISTS (
  SELECT 1 FROM turn_delegation_receipts_0165 WHERE turn_id = NEW.turn_id
)
BEGIN
  SELECT RAISE(ABORT, 'delegation receipt already exists for Turn');
END;
