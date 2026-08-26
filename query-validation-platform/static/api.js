// API 封装 + 公共工具（全局）
window.esc = function (s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
};
window.getUser = function () { try { return JSON.parse(localStorage.getItem('qvp_user') || 'null'); } catch (e) { return null; } };
window.setUser = function (u) { localStorage.setItem('qvp_user', JSON.stringify(u)); };
window.logout = function () { localStorage.removeItem('qvp_user'); location.hash = '#/login'; };
window.roleName = function (r) { return { A: '文案事实', B: '图片版权', C: '合规交付', admin: '管理员' }[r] || r; };
window.fmtTime = function (iso) { if (!iso) return '-'; try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }); } catch (e) { return iso; } };

// 任务状态 / 模式 / 风险 的展示口径（全局唯一，避免各视图各写一份）
window.STATUS = {
  draft:      { label: '排队中', cls: 'tag-gray' },
  processing: { label: '生产中', cls: 'tag-blue' },
  review:     { label: '待审核', cls: 'tag-yellow' },
  approved:   { label: '已通过', cls: 'tag-green' },
  rejected:   { label: '已驳回', cls: 'tag-red' },
  failed:     { label: '失败',   cls: 'tag-red' },
  trashed:    { label: '回收站', cls: 'tag-gray' },
};
window.MODE = {
  general: { label: '通用', desc: '通用科普/教程，纯文生图' },
  single:  { label: '单品', desc: '单一产品深测，搜实景图做图生图参考' },
  compare: { label: '对比', desc: '两主体对比，搜实景图做图生图参考' },
};
window.RISK = { green: { label: '绿', cls: 'tag-green' }, yellow: { label: '黄', cls: 'tag-yellow' }, red: { label: '红', cls: 'tag-red' } };
// 风险原因代码 → 中文说明（src/risk/classifier.py + src/quality/rules.py 的代码集，未知代码原样显示）
window.RISK_REASON = {
  p0_issue: '存在未关闭的 P0 问题单（事实/合规硬伤）',
  ocr_mismatch: '图文不一致（OCR 识别文字与分页文案不匹配）',
  evidence_incomplete: '证据链不完整（事实点缺少来源支撑）',
  word_count_400_700: '正文字数不在 400–700 字区间',
  title_max_25: '标题超过 25 字',
  no_absolute_words: '含绝对化用语（最/第一/100%/唯一 等）',
  has_disclaimer: '含安全/疗效类表述但缺少免责声明',
};
window.riskReason = function (code) { return RISK_REASON[code] || code; };

// 通用客户端排序：rows 按 key 排序返回新数组（order: asc/desc），null/undefined 恒排最后。
// 各视图表格「点击表头排序」的统一实现；服务端分页的列表（任务中心/回收站）走 sort/order 参数。
window.sortRows = function (rows, key, order) {
  const dir = order === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = a[key], vb = b[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb), 'zh-CN') * dir;
  });
};

window.api = {
  async _req(method, url, body, form) {
    const opt = { method, headers: {} };
    if (form) { opt.body = form; }
    else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const res = await fetch(url, opt);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      let msg = data && (data.detail || data.error);
      if (Array.isArray(msg)) msg = msg.map(m => m.msg).join('; ');
      throw new Error(typeof msg === 'string' && msg ? msg : ('HTTP ' + res.status));
    }
    return data;
  },
  get(url) { return this._req('GET', url); },
  post(url, body) { return this._req('POST', url, body || {}); },
  postForm(url, formData) { return this._req('POST', url, undefined, formData); },
  del(url) { return this._req('DELETE', url); },
};
