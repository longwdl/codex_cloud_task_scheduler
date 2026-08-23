CREATE TABLE github_api_sweep_metrics (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
  sweep_status TEXT,
  error_code TEXT,
  command_count INTEGER NOT NULL CHECK (command_count >= 0),
  read_count INTEGER NOT NULL CHECK (read_count >= 0),
  write_count INTEGER NOT NULL CHECK (write_count >= 0),
  failure_count INTEGER NOT NULL CHECK (failure_count >= 0),
  elapsed_milliseconds INTEGER NOT NULL CHECK (elapsed_milliseconds >= 0),
  core_remaining INTEGER,
  core_limit INTEGER,
  core_reset_epoch INTEGER,
  graphql_remaining INTEGER,
  graphql_limit INTEGER,
  graphql_reset_epoch INTEGER,
  rate_limit_error TEXT CHECK (
    rate_limit_error IS NULL OR rate_limit_error = 'github_rate_limit_unavailable'
  ),
  CHECK (command_count = read_count + write_count),
  CHECK (failure_count <= command_count),
  CHECK (
    (outcome = 'success' AND sweep_status IS NOT NULL AND error_code IS NULL)
    OR
    (outcome = 'failure' AND sweep_status IS NULL AND error_code = 'github_sweep_failed')
  ),
  CHECK (
    (core_remaining IS NULL AND core_limit IS NULL AND core_reset_epoch IS NULL)
    OR
    (core_remaining IS NOT NULL AND core_limit IS NOT NULL
      AND core_reset_epoch IS NOT NULL AND core_remaining >= 0
      AND core_limit >= core_remaining AND core_reset_epoch >= 0)
  ),
  CHECK (
    (graphql_remaining IS NULL AND graphql_limit IS NULL AND graphql_reset_epoch IS NULL)
    OR
    (graphql_remaining IS NOT NULL AND graphql_limit IS NOT NULL
      AND graphql_reset_epoch IS NOT NULL AND graphql_remaining >= 0
      AND graphql_limit >= graphql_remaining AND graphql_reset_epoch >= 0)
  )
);

CREATE INDEX github_api_sweep_metrics_completed_idx
  ON github_api_sweep_metrics(completed_at DESC, sequence DESC);
