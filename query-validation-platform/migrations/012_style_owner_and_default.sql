-- 012：风格关键词库用户隔离 + 用户默认风格（2026-08-28）
-- style_keywords 加 owner_id：NULL=admin 公共库，非空=个人库（参照 prompt_templates
-- 的属主模式）。style_name 的全局 UNIQUE 改为「同一 owner 内唯一」——NULL owner
-- 之间也要唯一，用两个部分唯一索引实现。011 的存量条目 owner_id 为 NULL，
-- 自然归入公共库，无需回填。
-- users.default_style：个人默认生图风格（风格名），命中时 style_pick 直接使用、
-- 跳过 LLM 判定。

ALTER TABLE style_keywords ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id);
ALTER TABLE style_keywords DROP CONSTRAINT IF EXISTS style_keywords_style_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS style_keywords_owner_name_uidx
    ON style_keywords (owner_id, style_name) WHERE owner_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS style_keywords_public_name_uidx
    ON style_keywords (style_name) WHERE owner_id IS NULL;

ALTER TABLE users ADD COLUMN IF NOT EXISTS default_style TEXT;
