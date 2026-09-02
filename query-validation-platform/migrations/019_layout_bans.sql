-- 019：版式禁用（2026-09-02 用户反馈「修正重出的 P6 文字版式与整套完全不相符，
-- 要求永久禁止该版式」）。tasks.layout_bans JSONB：{页码str: [禁用槽位int]}。
-- 生图留白区（get_image_prompt 无字版）与文字合成落版（text_composite.composite_page）
-- 经 slot_for_page 统一取槽：命中禁用则顺轮换到下一个未禁用槽位，两边永远同步。
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS layout_bans JSONB;
