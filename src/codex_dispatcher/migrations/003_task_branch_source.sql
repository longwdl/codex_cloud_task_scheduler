ALTER TABLE work_items
ADD COLUMN task_branch_source TEXT NOT NULL DEFAULT 'derived'
CHECK (task_branch_source IN ('derived', 'migrated'));
