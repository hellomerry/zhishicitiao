-- 013: 实图记录来源搜索词（2026-08-28 用户要求：实图要标识是哪个关键字搜来的）
-- entity_bind 自动搜/补搜、人工重搜均落库实际搜索词；手动上传为 NULL（无搜索词）。
ALTER TABLE assets ADD COLUMN IF NOT EXISTS search_query TEXT;
