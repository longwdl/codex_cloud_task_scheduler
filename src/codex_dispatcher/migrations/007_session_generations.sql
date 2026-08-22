CREATE TABLE session_generations (
  session_generation_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  generation_number INTEGER NOT NULL CHECK (generation_number > 0),
  state TEXT NOT NULL CHECK (state IN ('planned', 'starting', 'active', 'retiring', 'retired', 'failed')),
  role TEXT NOT NULL CHECK (role IN ('implementation', 'audit', 'ci_repair', 'salvage')),
  codex_session_id TEXT UNIQUE,
  start_head_sha TEXT NOT NULL,
  last_published_sha TEXT,
  policy_sha256 TEXT,
  rotation_reason TEXT,
  baseline_issue_revision TEXT,
  baseline_issue_content_sha256 TEXT,
  baseline_task_spec_sha256 TEXT,
  baseline_prompt_sha256 TEXT,
  baseline_approved_comment_ids_json TEXT,
  baseline_approved_context_sha256 TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  retired_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  UNIQUE(work_item_id, generation_number),
  CHECK (
    policy_sha256 IS NOT NULL
    OR COALESCE(rotation_reason = 'legacy_migration', 0)
  ),
  CHECK (
    (baseline_issue_revision IS NULL AND baseline_issue_content_sha256 IS NULL
      AND baseline_task_spec_sha256 IS NULL AND baseline_prompt_sha256 IS NULL
      AND baseline_approved_comment_ids_json IS NULL
      AND baseline_approved_context_sha256 IS NULL)
    OR
    (baseline_issue_revision IS NOT NULL AND baseline_issue_content_sha256 IS NOT NULL
      AND baseline_task_spec_sha256 IS NOT NULL AND baseline_prompt_sha256 IS NOT NULL
      AND baseline_approved_comment_ids_json IS NOT NULL
      AND baseline_approved_context_sha256 IS NOT NULL)
  ),
  CHECK (state != 'active' OR codex_session_id IS NOT NULL),
  CHECK (
    state NOT IN ('starting', 'active', 'retiring', 'retired')
    OR started_at IS NOT NULL
  ),
  CHECK (
    COALESCE(rotation_reason = 'legacy_migration', 0)
    OR state NOT IN ('starting', 'active', 'retiring', 'retired')
    OR baseline_issue_revision IS NOT NULL
  ),
  CHECK (
    (state IN ('retired', 'failed') AND retired_at IS NOT NULL)
    OR
    (state NOT IN ('retired', 'failed') AND retired_at IS NULL)
  )
);

CREATE UNIQUE INDEX one_live_session_generation_per_work_item
ON session_generations(work_item_id)
WHERE state IN ('planned', 'starting', 'active', 'retiring');

CREATE TABLE turn_session_generations (
  turn_id TEXT PRIMARY KEY,
  session_generation_id TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  FOREIGN KEY(session_generation_id) REFERENCES session_generations(session_generation_id)
);

CREATE INDEX turn_session_generations_by_generation
ON turn_session_generations(session_generation_id);

CREATE TABLE turn_usage (
  turn_id TEXT PRIMARY KEY,
  input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
  cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
  cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
  output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
  reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);

CREATE TABLE turn_prompt_inputs (
  turn_id TEXT PRIMARY KEY,
  prompt_kind TEXT NOT NULL CHECK (prompt_kind IN ('full', 'delta')),
  issue_content_sha256 TEXT NOT NULL,
  task_spec_sha256 TEXT NOT NULL,
  cumulative_approved_context_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
);

INSERT INTO session_generations (
  session_generation_id,
  work_item_id,
  generation_number,
  state,
  role,
  codex_session_id,
  start_head_sha,
  last_published_sha,
  policy_sha256,
  rotation_reason,
  created_at,
  started_at,
  retired_at,
  updated_at
)
SELECT
  'sg_' || substr(work_item_id, 4),
  work_item_id,
  1,
  'active',
  'implementation',
  codex_session_id,
  base_sha,
  last_published_sha,
  NULL,
  'legacy_migration',
  created_at,
  created_at,
  NULL,
  updated_at
FROM work_items
WHERE codex_session_id IS NOT NULL;

INSERT INTO turn_session_generations (turn_id, session_generation_id)
SELECT turns.turn_id, 'sg_' || substr(turns.work_item_id, 4)
FROM turns
JOIN work_items ON work_items.work_item_id = turns.work_item_id
WHERE work_items.codex_session_id IS NOT NULL;
