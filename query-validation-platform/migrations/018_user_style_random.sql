-- 018：随机风格模式（2026-09-02 用户要求「不想调风格的人也有开关」）
-- users.style_random=TRUE 时，pick_image_style 跳过 LLM 判定与偏好学习，
-- 每条任务从可用候选（个人库→公共库→内置 10 风格）均匀随机选一种——
-- 批量导入的任务风格自然各异。优先级低于钉选默认风格（显式选择永远优先）。
ALTER TABLE users ADD COLUMN IF NOT EXISTS style_random BOOLEAN NOT NULL DEFAULT FALSE;
