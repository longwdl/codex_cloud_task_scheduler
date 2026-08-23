CREATE TABLE turn_followup_intents (
  source_turn_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  cause TEXT NOT NULL CHECK (cause IN ('agent_checkpoint', 'ci_failure', 'audit_gap')),
  target_role TEXT NOT NULL CHECK (target_role IN ('implementation', 'ci_repair')),
  state TEXT NOT NULL CHECK (state IN ('planned', 'started', 'exhausted')),
  head_sha TEXT NOT NULL,
  context_json TEXT NOT NULL,
  context_sha256 TEXT NOT NULL,
  target_session_generation_id TEXT,
  target_turn_id TEXT UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  FOREIGN KEY(target_session_generation_id) REFERENCES session_generations(session_generation_id),
  FOREIGN KEY(target_turn_id) REFERENCES turns(turn_id),
  CHECK (
    state != 'started'
    OR (target_session_generation_id IS NOT NULL AND target_turn_id IS NOT NULL)
  )
);

CREATE UNIQUE INDEX one_planned_followup_per_work_item
ON turn_followup_intents(work_item_id)
WHERE state = 'planned';
