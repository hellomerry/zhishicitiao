-- 010：任务级生图模型选择（2026-08-28）
-- 默认 NULL = 用全局配置（gpt-image-2，openai_images）；用户在「待生图」确认
-- 环节手动选择其它模型（如 gemini）时才写入。生图/重生成/成本取价均按任务值。
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS image_provider TEXT;
