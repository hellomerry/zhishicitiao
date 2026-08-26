-- 009：配图版本保留（2026-08-26）
-- 定点重生成不再删除旧图，旧版降级为历史版本（is_active=false）保留，
-- 任务详情可对比新旧版本、可把历史版本一键换回正式。
-- 导出/OCR/交叉校验/交付校验只认 is_active=true 的正式版。
ALTER TABLE assets ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
