-- 回收站：任务软删除（status="trashed" + 原状态/时间/操作人），幂等可重复执行
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS prev_status TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS trashed_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS trashed_by TEXT;
