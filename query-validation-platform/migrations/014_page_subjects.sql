-- 014：分页画面主体（2026-08-31，用户反馈「图文不对应，general 无参考图最严重」）
-- asset_gen 生图前用一次 LLM 从 6 页分页文案提取每页画面主体（JSON 数组，6 个
-- 短字符串），注入生图提示词替换通用主体锚定条款；NULL = 未提取或提取失败。
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS page_subjects JSONB;
