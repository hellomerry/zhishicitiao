-- 020 场景化视觉扩写（2026-09-02，借鉴 8003 栈 visual_writer 实测效果）
-- tasks.visual_json：asset_gen 把 6 页文案扩写为英文视觉描述（含全套统一
-- 风格英文版 style_en + 每页视觉方向 pages[6]），冻结在任务上——定点重生成
-- 沿用快照不重新扩写，避免 LLM 非确定性导致重出页与整套漂移（与 014/015/017
-- 同一冻结哲学）。
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS visual_json JSONB;

-- visual_notes：视觉反馈笔记池（8003 用 nanobot 会话记忆，此处用 DB 实现，
-- 不引入外部依赖）。审核驳回理由/配图修正标记自动沉淀，扩写器每次注入最近
-- 若干条，让视觉方向越用越贴合团队口味。
CREATE TABLE IF NOT EXISTS visual_notes (
    id SERIAL PRIMARY KEY,
    note TEXT NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'review',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
