ALTER TABLE turns
ADD COLUMN issue_allowed_paths_json TEXT NOT NULL DEFAULT '[]';
