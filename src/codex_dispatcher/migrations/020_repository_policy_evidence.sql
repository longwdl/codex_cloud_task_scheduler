ALTER TABLE work_items
ADD COLUMN repository_policy_sha256 TEXT
CHECK (
  repository_policy_sha256 IS NULL
  OR (
    length(repository_policy_sha256) = 64
    AND repository_policy_sha256 NOT GLOB '*[^0-9a-f]*'
  )
);

CREATE TABLE repository_claim_policies (
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number > 0),
  issue_node_id TEXT NOT NULL,
  repository_class TEXT NOT NULL CHECK (repository_class IN ('fixture', 'higher-value')),
  recovery_profile TEXT NOT NULL CHECK (recovery_profile IN ('fixture-live-v1', 'higher-value-live-v1')),
  target_readback_profile TEXT NOT NULL CHECK (target_readback_profile IN ('fixture-exact-v1', 'higher-value-exact-v1')),
  admission_matrix_version INTEGER NOT NULL CHECK (admission_matrix_version > 0),
  admission_matrix_sha256 TEXT NOT NULL CHECK (
    length(admission_matrix_sha256) = 64
    AND admission_matrix_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  policy_sha256 TEXT NOT NULL UNIQUE CHECK (
    length(policy_sha256) = 64
    AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  work_item_id TEXT UNIQUE,
  created_at TEXT NOT NULL,
  bound_at TEXT,
  PRIMARY KEY(repository, issue_number),
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  CHECK ((work_item_id IS NULL) = (bound_at IS NULL))
);

CREATE TRIGGER repository_claim_policies_no_update
BEFORE UPDATE ON repository_claim_policies
WHEN NOT (
  OLD.work_item_id IS NULL
  AND OLD.bound_at IS NULL
  AND NEW.repository = OLD.repository
  AND NEW.issue_number = OLD.issue_number
  AND NEW.issue_node_id = OLD.issue_node_id
  AND NEW.repository_class = OLD.repository_class
  AND NEW.recovery_profile = OLD.recovery_profile
  AND NEW.target_readback_profile = OLD.target_readback_profile
  AND NEW.admission_matrix_version = OLD.admission_matrix_version
  AND NEW.admission_matrix_sha256 = OLD.admission_matrix_sha256
  AND NEW.policy_sha256 = OLD.policy_sha256
  AND NEW.created_at = OLD.created_at
  AND NEW.work_item_id IS NOT NULL
  AND NEW.bound_at IS NOT NULL
)
BEGIN
  SELECT RAISE(ABORT, 'repository claim policy is immutable');
END;

CREATE TRIGGER repository_claim_policies_no_delete
BEFORE DELETE ON repository_claim_policies
BEGIN
  SELECT RAISE(ABORT, 'repository claim policy cannot be deleted');
END;

CREATE TABLE repository_recovery_receipts (
  receipt_sha256 TEXT PRIMARY KEY CHECK (
    length(receipt_sha256) = 64 AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  work_item_id TEXT,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number > 0),
  policy_sha256 TEXT CHECK (
    policy_sha256 IS NULL OR (
      length(policy_sha256) = 64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  recovery_profile TEXT NOT NULL CHECK (
    recovery_profile IN (
      'fixture-live-v1', 'higher-value-live-v1', 'legacy-unbound-recovery-v1'
    )
  ),
  action TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('allowed', 'blocked')),
  code TEXT,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
);

CREATE TRIGGER repository_recovery_receipts_no_update
BEFORE UPDATE ON repository_recovery_receipts
BEGIN
  SELECT RAISE(ABORT, 'repository recovery receipt is immutable');
END;

CREATE TRIGGER repository_recovery_receipts_exact_work_item
BEFORE INSERT ON repository_recovery_receipts
WHEN NEW.work_item_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM work_items
  WHERE work_item_id = NEW.work_item_id
    AND repository = NEW.repository
    AND issue_number = NEW.issue_number
)
BEGIN
  SELECT RAISE(ABORT, 'repository recovery receipt target conflicts');
END;

CREATE TRIGGER repository_recovery_receipts_no_delete
BEFORE DELETE ON repository_recovery_receipts
BEGIN
  SELECT RAISE(ABORT, 'repository recovery receipt cannot be deleted');
END;

CREATE TABLE repository_target_readback_verdicts (
  verdict_sha256 TEXT PRIMARY KEY CHECK (
    length(verdict_sha256) = 64 AND verdict_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  work_item_id TEXT NOT NULL,
  policy_sha256 TEXT CHECK (
    policy_sha256 IS NULL OR (
      length(policy_sha256) = 64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
    )
  ),
  target_readback_profile TEXT NOT NULL CHECK (
    target_readback_profile IN (
      'fixture-exact-v1', 'higher-value-exact-v1', 'legacy-exact-recovery-v1'
    )
  ),
  action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('passed', 'blocked')),
  code TEXT,
  evidence_json TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL CHECK (
    length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  UNIQUE(work_item_id, action, evidence_sha256)
);

CREATE TRIGGER repository_target_readback_verdicts_no_update
BEFORE UPDATE ON repository_target_readback_verdicts
BEGIN
  SELECT RAISE(ABORT, 'repository target readback verdict is immutable');
END;

CREATE TRIGGER repository_target_readback_verdicts_no_delete
BEFORE DELETE ON repository_target_readback_verdicts
BEGIN
  SELECT RAISE(ABORT, 'repository target readback verdict cannot be deleted');
END;
