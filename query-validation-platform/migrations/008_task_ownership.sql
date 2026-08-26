-- 008：任务归属隔离（2026-08-26）
-- 历史任务 created_by 为空，回填为 admin 所有（普通用户不再看到历史任务，
-- admin 照常可见；导入接口此后都会写入 created_by）。
UPDATE tasks
SET created_by = (SELECT id FROM users WHERE name = 'admin' AND role = 'admin' LIMIT 1)
WHERE created_by IS NULL
  AND EXISTS (SELECT 1 FROM users WHERE name = 'admin' AND role = 'admin');
