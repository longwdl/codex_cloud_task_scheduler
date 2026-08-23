CREATE TABLE health_alert_deliveries (
  delivery_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('alert', 'recovery')),
  fingerprint TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  thread_ts TEXT,
  report_text TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('prepared', 'delivered')),
  message_ts TEXT,
  permalink TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(channel_id, message_ts),
  CHECK (length(fingerprint) = 64),
  CHECK (length(payload_sha256) = 64),
  CHECK (length(report_text) BETWEEN 1 AND 3000),
  CHECK (
    (kind = 'alert' AND thread_ts IS NULL)
    OR
    (kind = 'recovery' AND thread_ts IS NOT NULL)
  ),
  CHECK (
    (state = 'prepared' AND message_ts IS NULL AND permalink IS NULL)
    OR
    (state = 'delivered' AND message_ts IS NOT NULL AND permalink IS NOT NULL)
  )
);

CREATE TABLE active_health_alert (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  fingerprint TEXT NOT NULL,
  delivery_key TEXT NOT NULL UNIQUE,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(delivery_key) REFERENCES health_alert_deliveries(delivery_key),
  CHECK (length(fingerprint) = 64)
);
