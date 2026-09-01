// 主框架：侧边栏 + 顶栏 + 内容插槽
// 新用户风格开局引导（2026-09-02）：个人库为空且未钉默认风格时弹模板多选，
// 克隆 1-3 个内置模板到个人库（第一个选中的钉为默认），「暂时跳过」仅本次会话不再弹
const AppLayout = {
  props: { title: { type: String, default: '' } },
  data() {
    return {
      obShow: false, obTemplates: [], obSelected: [], obError: '', obSaving: false,
    };
  },
  computed: {
    user() { return getUser(); },
    menu() {
      const items = [
        { path: '/', icon: '📈', label: '工作台' },
        { path: '/import', icon: '📥', label: '任务导入' },
        { path: '/tasks', icon: '🗂️', label: '任务中心' },
        { path: '/monitor', icon: '📡', label: '实时监控' },
        { path: '/review', icon: '📋', label: '任务审核' },
        { path: '/sample', icon: '🎲', label: '随机抽查' },
        { path: '/trash', icon: '🗑️', label: '回收站' },
        { path: '/settings', icon: '🔑', label: '我的设置' },
        { path: '/users', icon: '👥', label: '用户管理', admin: true },
        { path: '/admin', icon: '⚙️', label: '系统管理', admin: true },
      ];
      return items.filter(i => !i.admin || (this.user && this.user.role === 'admin'));
    },
  },
  methods: {
    roleName,
    doLogout() { logout(); },
    // ---------- 新用户风格开局引导 ----------
    async checkOnboarding() {
      const u = getUser();
      if (!u || sessionStorage.getItem('qvp_ob_skip')) return;
      try {
        const st = await api.get('/api/styles/onboarding_state?actor=' + encodeURIComponent(u.name));
        if (!st.needs_onboarding) return;
        const t = await api.get('/api/styles/templates');
        this.obTemplates = t.items || [];
        this.obShow = true;
      } catch (e) { /* 引导加载失败不阻塞主界面 */ }
    },
    obToggle(t) {
      const i = this.obSelected.indexOf(t.style_name);
      if (i >= 0) { this.obSelected.splice(i, 1); this.obError = ''; return; }
      if (this.obSelected.length >= 3) { this.obError = '最多选 3 种风格'; return; }
      this.obError = '';
      this.obSelected.push(t.style_name);
    },
    obSkip() { sessionStorage.setItem('qvp_ob_skip', '1'); this.obShow = false; },
    async obSubmit() {
      if (!this.obSelected.length) { this.obError = '请至少选择 1 种风格'; return; }
      this.obSaving = true; this.obError = '';
      try {
        const r = await api.post('/api/styles/clone_templates', {
          actor: this.user.name, style_names: this.obSelected, pin: this.obSelected[0],
        });
        this.obShow = false;
        alert(`已加入 ${r.cloned.length} 种风格`
          + (r.skipped && r.skipped.length ? `（${r.skipped.join('、')} 已存在，跳过）` : '')
          + (r.default_style ? `，默认风格「${r.default_style}」` : ''));
      } catch (e) { this.obError = e.message; }
      finally { this.obSaving = false; }
    },
  },
  mounted() { this.checkOnboarding(); },
  template: `
  <div>
    <aside class="sidebar">
      <div class="brand"><img src="/static/logo.png" alt="logo" class="brand-logo">图文生产平台</div>
      <div class="menu-label">功能菜单</div>
      <router-link v-for="i in menu" :key="i.path" :to="i.path" class="menu-item" exact-active-class="active">
        <span class="icon">{{ i.icon }}</span>{{ i.label }}
      </router-link>
      <div class="sidebar-footer">
        <div class="user-box" v-if="user">
          <span class="role-badge">{{ user.role }}</span>
          <span class="name">{{ user.name }} · {{ roleName(user.role) }}</span>
          <button class="logout" @click="doLogout">退出</button>
        </div>
      </div>
    </aside>
    <div class="topbar"><div class="page-title">{{ title }}</div></div>
    <div class="content"><slot /></div>

    <div v-if="obShow" class="drawer-mask">
      <div class="export-modal" style="width:min(720px,94vw);top:8vh">
        <div class="drawer-head">
          <h2>🎨 选择你的图文风格</h2>
        </div>
        <p class="muted">挑 1-3 种你喜欢的风格作为起点（第一个选中的会成为默认风格）。之后随时可在「我的设置 → 风格关键词库」里调整，或上传样例图学习新风格。</p>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:14px 0">
          <div v-for="t in obTemplates" :key="t.style_name" @click="obToggle(t)"
               style="border-radius:8px;padding:10px 12px;cursor:pointer;background:var(--card)"
               :style="{border: obSelected.includes(t.style_name) ? '2px solid var(--primary)' : '1px solid var(--border)'}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
              <b>{{ t.style_name }}</b>
              <span v-if="obSelected.indexOf(t.style_name) === 0 && obSelected.length" class="tag tag-blue">默认</span>
              <span v-else-if="obSelected.includes(t.style_name)" class="tag tag-green">已选</span>
            </div>
            <div style="margin:6px 0;display:flex;flex-wrap:wrap;gap:4px">
              <span v-for="k in (t.keywords || '').split(/[,，]/).map(s => s.trim()).filter(Boolean)"
                    :key="k" class="tag tag-gray" style="font-size:11px">{{ k }}</span>
            </div>
            <p class="muted" style="font-size:12px;margin:0">{{ t.description }}</p>
          </div>
        </div>
        <p v-if="obError" class="form-error">{{ obError }}</p>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
          <span class="muted" style="font-size:13px">已选 {{ obSelected.length }}/3<template v-if="obSelected.length">，默认：{{ obSelected[0] }}</template></span>
          <div style="display:flex;gap:8px">
            <button class="btn btn-outline btn-sm" @click="obSkip">暂时跳过</button>
            <button class="btn btn-primary btn-sm" :disabled="obSaving || !obSelected.length" @click="obSubmit">{{ obSaving ? '保存中…' : '开始使用' }}</button>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
