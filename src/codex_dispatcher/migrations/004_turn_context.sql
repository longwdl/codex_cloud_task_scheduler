ALTER TABLE turns
ADD COLUMN included_comment_ids_json TEXT NOT NULL DEFAULT '[]';
