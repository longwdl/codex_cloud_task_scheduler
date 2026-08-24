CREATE TABLE work_item_discard_requests (
  work_item_id TEXT PRIMARY KEY,
  expected_head_sha TEXT NOT NULL,
  pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
  requested_by TEXT NOT NULL,
  request_event_id TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
);

INSERT INTO work_item_discard_requests (
  work_item_id,
  expected_head_sha,
  pr_number,
  requested_by,
  request_event_id,
  requested_at,
  request_sha256,
  created_at
)
SELECT
  work_item_id,
  expected_head_sha,
  pr_number,
  requested_by,
  request_event_id,
  requested_at,
  request_sha256,
  created_at
FROM work_item_dispositions;

CREATE TRIGGER discard_request_is_immutable_update
BEFORE UPDATE ON work_item_discard_requests
BEGIN
  SELECT RAISE(ABORT, 'discard request is immutable');
END;

CREATE TRIGGER discard_request_is_immutable_delete
BEFORE DELETE ON work_item_discard_requests
BEGIN
  SELECT RAISE(ABORT, 'discard request is immutable');
END;

CREATE TRIGGER discard_request_rejects_turn_insert
BEFORE INSERT ON turns
WHEN EXISTS (
  SELECT 1 FROM work_item_discard_requests
  WHERE work_item_id = NEW.work_item_id
)
BEGIN
  SELECT RAISE(ABORT, 'discard-requested WorkItem cannot receive a Turn');
END;

CREATE TRIGGER discard_request_rejects_generation_insert
BEFORE INSERT ON session_generations
WHEN EXISTS (
  SELECT 1 FROM work_item_discard_requests
  WHERE work_item_id = NEW.work_item_id
)
BEGIN
  SELECT RAISE(ABORT, 'discard-requested WorkItem cannot receive a session generation');
END;

CREATE TRIGGER discard_request_freezes_work_item_execution
BEFORE UPDATE ON work_items
WHEN EXISTS (
  SELECT 1 FROM work_item_discard_requests
  WHERE work_item_id = NEW.work_item_id
)
AND (
  (NEW.state != OLD.state AND NEW.state IN (
    'preparing', 'ready', 'running', 'waiting_input'
  ))
  OR NEW.last_published_sha IS NOT OLD.last_published_sha
  OR NEW.pr_number IS NOT OLD.pr_number
)
BEGIN
  SELECT RAISE(ABORT, 'discard-requested WorkItem execution is frozen');
END;

CREATE TABLE terminal_github_closures (
  work_item_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (
    kind IN ('completed_issue', 'discarded_pull_request', 'discarded_issue')
  ),
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number > 0),
  issue_node_id TEXT NOT NULL,
  pr_number INTEGER CHECK (pr_number IS NULL OR pr_number > 0),
  expected_head_sha TEXT NOT NULL,
  close_reason TEXT CHECK (
    close_reason IS NULL OR close_reason IN ('completed', 'not_planned')
  ),
  state TEXT NOT NULL CHECK (state IN ('prepared', 'completed', 'blocked')),
  request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('closed', 'already_closed')),
  error_code TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(work_item_id, kind),
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  CHECK (
    (kind = 'discarded_pull_request' AND pr_number IS NOT NULL AND close_reason IS NULL)
    OR
    (kind = 'completed_issue' AND pr_number IS NULL AND close_reason = 'completed')
    OR
    (kind = 'discarded_issue' AND pr_number IS NULL AND close_reason = 'not_planned')
  ),
  CHECK (
    (state = 'prepared' AND outcome IS NULL AND error_code IS NULL AND completed_at IS NULL)
    OR
    (state = 'completed' AND outcome IS NOT NULL AND error_code IS NULL AND completed_at IS NOT NULL)
    OR
    (state = 'blocked' AND outcome IS NULL AND error_code IS NOT NULL AND completed_at IS NULL)
  )
);

CREATE INDEX terminal_github_closures_state_idx
  ON terminal_github_closures(state, kind, updated_at);

CREATE TRIGGER terminal_github_closure_rejects_delete
BEFORE DELETE ON terminal_github_closures
BEGIN
  SELECT RAISE(ABORT, 'terminal GitHub closure is permanent');
END;

CREATE TRIGGER terminal_github_closure_identity_is_immutable
BEFORE UPDATE ON terminal_github_closures
WHEN
  OLD.state != 'prepared'
  OR NEW.work_item_id IS NOT OLD.work_item_id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.repository IS NOT OLD.repository
  OR NEW.issue_number IS NOT OLD.issue_number
  OR NEW.issue_node_id IS NOT OLD.issue_node_id
  OR NEW.pr_number IS NOT OLD.pr_number
  OR NEW.expected_head_sha IS NOT OLD.expected_head_sha
  OR NEW.close_reason IS NOT OLD.close_reason
  OR NEW.request_sha256 IS NOT OLD.request_sha256
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.state NOT IN ('completed', 'blocked')
BEGIN
  SELECT RAISE(ABORT, 'terminal GitHub closure identity is immutable');
END;
