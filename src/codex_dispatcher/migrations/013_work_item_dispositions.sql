CREATE TEMP TABLE migration_013_guard (
  invalid_count INTEGER NOT NULL CHECK (invalid_count = 0)
);

INSERT INTO migration_013_guard(invalid_count)
SELECT COUNT(*)
FROM work_items
WHERE state = 'completed'
  AND (
    EXISTS (
      SELECT 1 FROM turns
      WHERE turns.work_item_id = work_items.work_item_id
        AND turns.state IN (
          'planned', 'starting', 'running', 'reconciling', 'checkpointing', 'published'
        )
    )
    OR EXISTS (
      SELECT 1 FROM session_generations
      WHERE session_generations.work_item_id = work_items.work_item_id
        AND (
          session_generations.state IN ('planned', 'starting')
          OR (
            session_generations.state IN ('active', 'retiring')
            AND COALESCE(
              session_generations.last_published_sha,
              session_generations.start_head_sha
            ) != COALESCE(work_items.last_published_sha, work_items.base_sha)
          )
          OR (
            session_generations.state IN ('active', 'retiring')
            AND NOT EXISTS (
              SELECT 1 FROM work_item_events
              WHERE work_item_events.work_item_id = work_items.work_item_id
                AND work_item_events.event_type = 'work_item_state_changed'
                AND work_item_events.payload_json =
                  '{"from":"review","to":"completed"}'
            )
          )
        )
    )
  );

DROP TABLE migration_013_guard;

CREATE TABLE work_item_dispositions (
  work_item_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('abandoned', 'superseded')),
  expected_head_sha TEXT NOT NULL,
  pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
  requested_by TEXT NOT NULL,
  request_event_id TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  eligible_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  CHECK ((kind = 'abandoned') = (pr_number IS NULL))
);

CREATE TABLE work_item_absence_reconciliations (
  work_item_id TEXT PRIMARY KEY,
  expected_head_sha TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  observed_by TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_item_archives(work_item_id)
);

CREATE TRIGGER disposed_work_item_is_immutable
BEFORE UPDATE ON work_items
WHEN EXISTS (
  SELECT 1 FROM work_item_dispositions
  WHERE work_item_id = OLD.work_item_id
)
BEGIN
  SELECT RAISE(ABORT, 'disposed WorkItem is immutable');
END;

CREATE TRIGGER disposed_work_item_rejects_turn_insert
BEFORE INSERT ON turns
WHEN EXISTS (
  SELECT 1 FROM work_item_dispositions
  WHERE work_item_id = NEW.work_item_id
)
BEGIN
  SELECT RAISE(ABORT, 'disposed WorkItem cannot receive a Turn');
END;

CREATE TRIGGER disposed_work_item_rejects_generation_insert
BEFORE INSERT ON session_generations
WHEN EXISTS (
  SELECT 1 FROM work_item_dispositions
  WHERE work_item_id = NEW.work_item_id
)
BEGIN
  SELECT RAISE(ABORT, 'disposed WorkItem cannot receive a session generation');
END;

INSERT INTO work_item_events (
  work_item_id,
  turn_id,
  event_type,
  event_time,
  payload_json
)
SELECT
  session_generations.work_item_id,
  NULL,
  'session_generation_state_changed',
  (
    SELECT event_time
    FROM work_item_events AS completion_events
    WHERE completion_events.work_item_id = session_generations.work_item_id
      AND completion_events.event_type = 'work_item_state_changed'
      AND completion_events.payload_json = '{"from":"review","to":"completed"}'
    ORDER BY event_id DESC
    LIMIT 1
  ),
  '{"from":"' || session_generations.state
    || '","reason":"completed_migration_backfill","session_generation_id":"'
    || session_generations.session_generation_id || '","to":"retired"}'
FROM session_generations
JOIN work_items USING(work_item_id)
WHERE work_items.state = 'completed'
  AND session_generations.state IN ('active', 'retiring')
  AND EXISTS (
    SELECT 1
    FROM work_item_events AS completion_events
    WHERE completion_events.work_item_id = session_generations.work_item_id
      AND completion_events.event_type = 'work_item_state_changed'
      AND completion_events.payload_json = '{"from":"review","to":"completed"}'
  );

UPDATE session_generations
SET
  state = 'retired',
  retired_at = (
    SELECT event_time
    FROM work_item_events AS completion_events
    WHERE completion_events.work_item_id = session_generations.work_item_id
      AND completion_events.event_type = 'work_item_state_changed'
      AND completion_events.payload_json = '{"from":"review","to":"completed"}'
    ORDER BY event_id DESC
    LIMIT 1
  ),
  updated_at = (
    SELECT event_time
    FROM work_item_events AS completion_events
    WHERE completion_events.work_item_id = session_generations.work_item_id
      AND completion_events.event_type = 'work_item_state_changed'
      AND completion_events.payload_json = '{"from":"review","to":"completed"}'
    ORDER BY event_id DESC
    LIMIT 1
  )
WHERE work_item_id IN (
  SELECT work_item_id FROM work_items WHERE state = 'completed'
)
  AND state IN ('active', 'retiring')
  AND EXISTS (
    SELECT 1
    FROM work_item_events AS completion_events
    WHERE completion_events.work_item_id = session_generations.work_item_id
      AND completion_events.event_type = 'work_item_state_changed'
      AND completion_events.payload_json = '{"from":"review","to":"completed"}'
  );
