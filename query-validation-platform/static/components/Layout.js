// 主框架：侧边栏 + 顶栏 + 内容插槽
// 新用户风格开局引导（2026-09-02）：个人库为空且未钉默认风格时弹模板多选，
// 克隆 1-3 个内置模板到个人库（第一个选中的钉为默认），「暂时跳过」仅本次会话不再弹；
// 侧边栏常驻「风格选择」按钮可随时手动打开同一弹窗（不写跳过标记）
const AppLayout = {
  props: { title: { type: String, default: '' } },
  data() {
    return {
      obShow: false, obTemplates: [], obSelected: [], obError: '', obSaving: false,
      obManual: false,   // true=侧边栏按钮手动打开（不写跳过标记，按钮文案为关闭/保存）
      obRandom: false,   // 随机风格模式已开启（迁移 018）：弹窗显示状态条而非模板选择
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
        this.obManual = false;
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
    obSkip() {
      // 手动打开时只是关闭弹窗；自动引导的「暂时跳过」才写会话标记
      if (!this.obManual) sessionStorage.setItem('qvp_ob_skip', '1');
      this.obShow = false;
    },
    // 侧边栏「风格选择」按钮：任何用户随时可开，回显个人库中已选的模板风格
    async openStylePicker() {
      const u = getUser();
      if (!u) return;
      this.obManual = true; this.obError = ''; this.obSelected = [];
      try {
        const st = await api.get('/api/styles/onboarding_state?actor=' + encodeURIComponent(u.name));
        this.obRandom = !!st.style_random;
        const t = await api.get('/api/styles/templates');
        this.obTemplates = t.items || [];
        const tnames = new Set(this.obTemplates.map(x => x.style_name));
        const mine = await api.get('/api/styles?actor=' + encodeURIComponent(u.name));
        this.obSelected = (mine.items || [])
          .filter(s => s.scope === 'mine' && tnames.has(s.style_name))
          .map(s => s.style_name).slice(0, 3);
        this.obShow = true;
      } catch (e) { this.obError = '风格模板加载失败：' + e.message; this.obShow = true; }
    },
    // 随机风格模式开关（2026-09-02，迁移 018）：不想挑风格的用户开启后每条任务
    // 随机选一种内置风格，批量任务篇篇不同；钉选的默认风格仍优先
    async obSetRandom(on) {
      try {
        await api.post('/api/styles/random_mode', { actor: this.user.name, enabled: on });
        this.obRandom = on;
        if (on) {
          this.obShow = false;
          alert('已开启随机风格模式：每条任务自动随机选一种风格，批量任务篇篇不同。\n想改回固定风格，点侧边栏「风格选择」关闭即可。');
        }
      } catch (e) { this.obError = e.message; }
    },
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
      <a class="menu-item" style="cursor:pointer" @click="openStylePicker"><span class="icon">🎨</span>风格选择</a>
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
        <div v-if="obRandom" style="border:1px solid var(--primary);border-radius:8px;padding:10px 12px;margin:0 0 12px;display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span style="font-size:13px">🎲 <b>随机风格模式已开启</b>：每条任务自动随机选一种风格，批量任务篇篇不同，无需挑选。</span>
          <button class="btn btn-outline btn-sm" @click="obSetRandom(false)">关闭随机，改挑模板</button>
        </div>
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
            <button v-if="!obRandom" class="btn btn-outline btn-sm" title="不想挑风格：每条任务随机选一种内置风格，批量任务篇篇不同" @click="obSetRandom(true)">🎲 随机来</button>
            <button class="btn btn-outline btn-sm" @click="obSkip">{{ obManual ? '关闭' : '暂时跳过' }}</button>
            <button class="btn btn-primary btn-sm" :disabled="obSaving || !obSelected.length" @click="obSubmit">{{ obSaving ? '保存中…' : (obManual ? '保存' : '开始使用') }}</button>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
