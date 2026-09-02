-- 016：风格变体轴（2026-09-02 反同质化方案 #2）
-- 风格 = 签名层（description，固定识别度）+ 变体层（variants，每任务采样轮换）。
-- variants 为变体句池（换行或中文分号分隔），asset_gen 选定风格时按 task_id
-- 采样一条追加进描述词快照——同一用户同一风格，每篇产出在签名内不重样；
-- 不设置则回退 style_pick 内置变体池。可重复执行。

ALTER TABLE style_keywords ADD COLUMN IF NOT EXISTS variants TEXT;
