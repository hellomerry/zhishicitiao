-- 011：风格关键词库 + 任务级生图风格（2026-08-28，移植自同事 8003 实现）
-- 风格关键词库：用户"知识训练"落地——风格→关键词集/描述词映射，生图前自动
-- 匹配视觉风格，描述词替换生图模板中的固定风格句；库为空回退内置 8 风格。
-- tasks.gen_image_style：asset_gen 节点生图前判定一次，6 张图共用同一风格。

CREATE TABLE IF NOT EXISTS style_keywords (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    style_name  TEXT NOT NULL,                       -- 风格名（如：科技蓝调）
    keywords    TEXT NOT NULL DEFAULT '',             -- 匹配关键词（逗号分隔，query/正文命中即加分）
    description TEXT NOT NULL DEFAULT '',             -- 描述词（注入生图提示词的视觉描述）
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (style_name)
);
CREATE INDEX IF NOT EXISTS style_keywords_enabled_idx ON style_keywords (enabled);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gen_image_style TEXT;
