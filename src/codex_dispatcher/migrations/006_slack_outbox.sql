CREATE TABLE slack_deliveries (
  deduplication_key TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  turn_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('root', 'status', 'result', 'question', 'failure')),
  channel_id TEXT NOT NULL,
  thread_ts TEXT,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('prepared', 'delivered')),
  message_ts TEXT,
  permalink TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  UNIQUE(channel_id, message_ts),
  CHECK (
    (kind = 'root' AND turn_id IS NULL AND thread_ts IS NULL)
    OR
    (kind != 'root' AND turn_id IS NOT NULL AND thread_ts IS NOT NULL)
  ),
  CHECK (
    (state = 'prepared' AND message_ts IS NULL AND permalink IS NULL)
    OR
    (state = 'delivered' AND message_ts IS NOT NULL AND permalink IS NOT NULL)
  )
);
