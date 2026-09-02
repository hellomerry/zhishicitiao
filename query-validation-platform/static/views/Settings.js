// 设置：修改自己的密码 + 提示词库（系统默认仅 admin 可改；自定义每人管自己的，admin 看全部）
const SettingsView = {
  data() {
    return {
      tab: 'prompts',
      // 风格关键词库（生图视觉风格自动匹配的训练数据，2026-08-28；迁移 012 起
      // 分「我的/公共」两区，公共区仅 admin 可写）
      styleItems: [],
      styleForm: { style_name: '', keywords: '', description: '', variants: '', enabled: true, public: false },
      styleImportMsg: '', styleImportPublic: false,
      // 样例图学习风格草稿（2026-09-02）：VL 提炼后填入下方表单，用户确认再保存
      styleLearnLoading: false, styleLearnMsg: '',
      // 我的风格偏好统计 + 个人默认风格（使用中学习，2026-08-28）
      styleStats: [], styleDefault: null, styleRandom: false,
      // 密码
      old_password: '', new_password: '', confirm: '', pwError: '', pwMsg: '', savingPw: false,
      // 提示词库
      catalog: [], customs: [],
      curStage: 'draft_gen', curMode: 'general',
      form: null, showSystem: false, sysForm: null,
      error: '', msg: '',
      // 工作日志
      logs: [], logTotal: 0, logActions: [], logUsers: [],
      logUser: '', logAction: '', logPage: 0, logLimit: 20, logsAdmin: false,
      logOrder: 'desc',                  // 服务端排序：desc 最新在前 / asc 最早在前
      // 提示词库表格排序（客户端）
      pSort: 'updated_at', pOrder: 'desc',
    };
  },
  computed: {
    user() { return getUser(); },
    isAdmin() { const u = getUser(); return u && u.role === 'admin'; },
    curStageObj() { return this.catalog.find(s => s.stage === this.curStage); },
    modes() { return this.curStageObj ? this.curStageObj.items : []; },
    curItem() { return this.modes.find(m => m.mode === this.curMode) || this.modes[0]; },
    // 当前 (环节, 模式) 下的自定义提示词；admin 看全部用户的
    filtered() {
      const m = this.curItem ? this.curItem.mode : null;
      const rows = this.customs.filter(p => p.stage === this.curStage && p.mode === m);
      return sortRows(rows, this.pSort, this.pOrder);
    },
    // 风格库分区（迁移 012）：我的 / 公共
    myStyles() { return this.styleItems.filter(r => r.scope === 'mine'); },
    publicStyles() { return this.styleItems.filter(r => r.scope === 'public'); },
    // 偏好建议：某风格任务数≥3 且通过率≥80% 且不是当前默认 → 建议钉为默认
    styleSuggestion() {
      return this.styleStats.find(s => s.total >= 3 && s.approval_rate !== null
        && s.approval_rate >= 0.8 && s.style_name !== this.styleDefault) || null;
    },
  },
  methods: {
    fmtTime,
    // ---------- 风格关键词库 ----------
    emptyStyleForm() {
      return { style_name: '', keywords: '', description: '', variants: '', enabled: true, public: false };
    },
    async loadStyles() {
      try {
        const r = await api.get('/api/styles?actor=' + encodeURIComponent(this.user.name));
        this.styleItems = r.items || [];
      } catch (e) { /* 静默 */ }
    },
    async loadStyleStats() {
      try {
        const r = await api.get('/api/styles/stats?actor=' + encodeURIComponent(this.user.name));
        this.styleStats = r.items || []; this.styleDefault = r.default_style || null;
        this.styleRandom = !!r.style_random;
      } catch (e) { /* 静默 */ }
    },
    // 随机风格模式开关（2026-09-02，迁移 018）：不想挑风格时开启，每条任务
    // 随机选一种风格、批量篇篇不同；钉选的默认风格仍优先
    async toggleStyleRandom() {
      try {
        const on = !this.styleRandom;
        await api.post('/api/styles/random_mode', { actor: this.user.name, enabled: on });
        this.styleRandom = on;
      } catch (e) { alert('设置失败：' + e.message); }
    },
    editStyle(r) { this.styleForm = { ...r, public: r.scope === 'public' }; },
    async saveStyle() {
      try {
        await api.post('/api/styles?actor=' + encodeURIComponent(this.user.name), this.styleForm);
        this.styleForm = this.emptyStyleForm();
        this.loadStyles();
      } catch (e) { alert('保存失败：' + e.message); }
    },
    async removeStyle(r) {
      if (!confirm(`删除风格「${r.style_name}」？`)) return;
      try { await api.del('/api/styles/' + r.id + '?actor=' + encodeURIComponent(this.user.name)); this.loadStyles(); }
      catch (e) { alert('删除失败：' + e.message); }
    },
    async importStyles(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f);
      fd.append('actor', this.user.name || 'anonymous');
      fd.append('public', this.styleImportPublic ? 'true' : 'false');
      try {
        const r = await api.postForm('/api/styles/import', fd);
        this.styleImportMsg = `导入 ${r.imported} 条` + (r.errors && r.errors.length ? `，${r.errors.length} 行失败` : '');
        this.loadStyles();
      } catch (e) { this.styleImportMsg = '导入失败：' + e.message; }
      ev.target.value = '';
    },
    // ---------- 样例图学习风格 ----------
    async learnStyle(ev) {
      const files = [...ev.target.files];
      ev.target.value = '';
      if (!files.length) return;
      if (files.length > 5) { this.styleLearnMsg = '最多选 5 张图'; return; }
      const big = files.find(f => f.size > 15 * 1024 * 1024);
      if (big) { this.styleLearnMsg = `「${big.name}」超过 15MB`; return; }
      const fd = new FormData();
      fd.append('actor', this.user.name || 'anonymous');
      files.forEach(f => fd.append('files', f));
      this.styleLearnLoading = true; this.styleLearnMsg = '';
      try {
        const r = await api.postForm('/api/styles/learn', fd);
        this.styleForm = { ...this.emptyStyleForm(), style_name: r.style_name,
                           keywords: r.keywords, description: r.description };
        this.styleLearnMsg = '已提炼风格草稿并填入下方表单，可修改后点「添加」保存到个人库';
      } catch (e) { this.styleLearnMsg = '学习失败：' + e.message; }
      finally { this.styleLearnLoading = false; }
    },
    // ---------- 使用中学习：默认风格 / 偏好建议 ----------
    async setDefaultStyle(name) {
      try {
        await api.post('/api/styles/default', { actor: this.user.name, style_name: name });
        this.loadStyleStats();
      } catch (e) { alert('设置默认风格失败：' + e.message); }
    },
    async clearDefaultStyle() {
      try {
        await api.del('/api/styles/default?actor=' + encodeURIComponent(this.user.name));
        this.loadStyleStats();
      } catch (e) { alert('取消默认风格失败：' + e.message); }
    },
    async prefillStyle(name) {
      // 「存为我的风格」：反查描述词预填表单，用户补匹配关键词后保存
      let desc = '';
      try {
        const r = await api.get('/api/styles/lookup?style_name=' + encodeURIComponent(name)
          + '&actor=' + encodeURIComponent(this.user.name));
        desc = r.description || '';
      } catch (e) { /* 描述词留空 */ }
      this.styleForm = { ...this.emptyStyleForm(), style_name: name, description: desc };
    },
    fmtRate(r) { return r === null || r === undefined ? '—' : Math.round(r * 100) + '%'; },
    toggleLogOrder() {
      this.logOrder = this.logOrder === 'desc' ? 'asc' : 'desc';
      this.logPage = 0; this.loadLogs();
    },
    sortPrompts(col) {
      if (this.pSort === col) {
        this.pOrder = this.pOrder === 'asc' ? 'desc' : 'asc';
      } else {
        this.pSort = col;
        this.pOrder = col === 'updated_at' ? 'desc' : 'asc';
      }
    },
    pMark(col) {
      if (this.pSort !== col) return '';
      return this.pOrder === 'asc' ? ' ▲' : ' ▼';
    },
    // ---------- 工作日志 ----------
    actLabel(a) {
      return {
        login: '登录', login_failed: '登录失败', register: '注册',
        change_password: '修改密码', import_tasks: '导入任务', retry_task: '重试任务',
        review_approve: '审核通过', review_reject: '审核驳回',
        prompt_create: '新建提示词', prompt_update: '修改提示词', prompt_delete: '删除提示词',
        system_prompt_update: '改系统提示词', system_prompt_restore: '恢复系统提示词',
        style_kb: '风格库维护',
        user_create: '新建用户', user_update: '修改用户', user_delete: '删除用户',
        admin_clear: '清空工作内容', admin_export: '导出工作记录',
        export_approved: '导出内容包',
        maintenance_start: '进入检修', maintenance_end: '结束检修',
      }[a] || a;
    },
    async loadLogs() {
      try {
        let url = '/api/activity?actor=' + encodeURIComponent(this.user.name)
          + '&limit=' + this.logLimit + '&offset=' + (this.logPage * this.logLimit);
        if (this.logUser) url += '&user=' + encodeURIComponent(this.logUser);
        if (this.logAction) url += '&action=' + encodeURIComponent(this.logAction);
        url += '&order=' + this.logOrder;
        const r = await api.get(url);
        this.logs = r.logs; this.logTotal = r.total;
        this.logActions = r.actions; this.logsAdmin = r.is_admin;
        if (r.is_admin && !this.logUsers.length) {
          const u = await api.get('/api/admin/users?actor=' + encodeURIComponent(this.user.name));
          this.logUsers = u.users.map(x => x.name);
        }
      } catch (e) { this.error = e.message; }
    },
    logPages() { return Math.max(1, Math.ceil(this.logTotal / this.logLimit)); },
    // ---------- 密码 ----------
    async submitPw() {
      this.pwError = ''; this.pwMsg = '';
      if (this.new_password !== this.confirm) { this.pwError = '两次输入的新密码不一致'; return; }
      this.savingPw = true;
      try {
        const r = await api.post('/api/auth/change_password', {
          username: this.user.name,
          old_password: this.old_password,
          new_password: this.new_password,
        });
        if (!r.ok) throw new Error(r.error || '修改失败');
        this.pwMsg = '密码已修改，下次登录请使用新密码';
        this.old_password = this.new_password = this.confirm = '';
      } catch (e) { this.pwError = e.message; }
      finally { this.savingPw = false; }
    },
    // ---------- 提示词库 ----------
    async load() {
      this.error = '';
      try {
        const [c, l] = await Promise.all([
          api.get('/api/prompts/catalog'),
          api.get('/api/prompts?actor=' + encodeURIComponent(this.user.name)),
        ]);
        this.catalog = c.stages; this.customs = l.prompts;
        if (this.curStageObj && !this.modes.some(m => m.mode === this.curMode)) {
          this.curMode = this.modes[0].mode;
        }
      } catch (e) { this.error = e.message; }
    },
    selectStage(s) {
      this.curStage = s;
      const st = this.catalog.find(x => x.stage === s);
      this.curMode = st && st.items.length ? st.items[0].mode : null;
      this.form = null; this.sysForm = null;
    },
    // 系统默认（仅 admin 可编辑/恢复）
    startSysEdit() { this.sysForm = { content: this.curItem ? this.curItem.system : '' }; },
    async saveSys() {
      this.error = ''; this.msg = '';
      try {
        await api._req('PUT', '/api/prompts/system', {
          actor: this.user.name, stage: this.curStage,
          mode: this.curItem ? this.curItem.mode : null,
          content: this.sysForm.content });
        this.msg = '系统默认提示词已更新，对所有用户生效';
        this.sysForm = null; await this.load();
      } catch (e) { this.error = e.message; }
    },
    async restoreSys() {
      if (!confirm('确定恢复为代码内置的系统默认提示词？当前的 admin 修改将被移除。')) return;
      this.error = ''; this.msg = '';
      try {
        const mode = this.curItem ? this.curItem.mode : null;
        await api._req('DELETE', '/api/prompts/system?actor=' + encodeURIComponent(this.user.name)
          + '&stage=' + this.curStage + (mode ? '&mode=' + mode : ''));
        this.msg = '已恢复内置默认'; await this.load();
      } catch (e) { this.error = e.message; }
    },
    // 自定义
    newPrompt() {
      this.form = {
        id: null, name: '', content: this.curItem ? this.curItem.system : '',
        is_active: false, stage: this.curStage,
        mode: this.curItem ? this.curItem.mode : null,
      };
    },
    editPrompt(p) {
      this.form = { id: p.id, name: p.name, content: p.content,
                    is_active: p.is_active, stage: p.stage, mode: p.mode };
    },
    canEdit(p) { return this.isAdmin || p.owner_name === this.user.name; },
    async save() {
      this.error = ''; this.msg = '';
      const f = this.form;
      try {
        if (f.id) {
          await api._req('PUT', '/api/prompts/' + f.id, {
            actor: this.user.name, name: f.name, content: f.content, is_active: f.is_active });
        } else {
          await api.post('/api/prompts', {
            actor: this.user.name, stage: f.stage, mode: f.mode,
            name: f.name, content: f.content, is_active: f.is_active });
        }
        this.msg = '已保存'; this.form = null; await this.load();
      } catch (e) { this.error = e.message; }
    },
    async toggle(p) {
      this.error = ''; this.msg = '';
      try {
        await api._req('PUT', '/api/prompts/' + p.id, {
          actor: this.user.name, is_active: !p.is_active });
        this.msg = p.is_active ? '已停用，回退系统默认' : '已启用，新任务将使用该提示词';
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    async del(p) {
      if (!confirm(`确定删除自定义提示词「${p.name}」？`)) return;
      this.error = ''; this.msg = '';
      try {
        await api._req('DELETE', '/api/prompts/' + p.id + '?actor=' + encodeURIComponent(this.user.name));
        this.msg = '已删除'; await this.load();
      } catch (e) { this.error = e.message; }
    },
  },
  mounted() {
    this.load();
    // 从任务详情「存为我的风格」跳入：预填风格表单并切到风格库页签
    try {
      const pre = JSON.parse(localStorage.getItem('qvp_style_prefill') || 'null');
      if (pre && pre.style_name) {
        this.tab = 'styles';
        this.styleForm = { ...this.emptyStyleForm(), ...pre };
        this.loadStyles(); this.loadStyleStats();
        localStorage.removeItem('qvp_style_prefill');
      }
    } catch (e) { /* 忽略坏数据 */ }
  },
  template: `
  <app-layout title="我的设置">
    <div class="card" style="padding:0 20px">
      <div class="tabs" style="margin-bottom:0">
        <button class="tab" :class="{on: tab==='prompts'}" @click="tab='prompts'">提示词库</button>
        <button class="tab" :class="{on: tab==='styles'}" @click="tab='styles'; loadStyles(); loadStyleStats()">风格关键词库</button>
        <button class="tab" :class="{on: tab==='logs'}" @click="tab='logs'; loadLogs()">工作日志</button>
        <button class="tab" :class="{on: tab==='password'}" @click="tab='password'">修改密码</button>
      </div>
    </div>

    <template v-if="tab==='styles'">
    <div class="card">
      <h2>我的风格偏好 <span class="muted" style="font-weight:normal;font-size:13px">按历史任务自动统计；钉选默认风格后生图直接使用、跳过自动判定</span></h2>
      <p style="margin:4px 0 0;font-size:13px">🎲 随机风格模式：<b :style="{color: styleRandom ? 'var(--primary)' : 'inherit'}">{{ styleRandom ? '已开启' : '已关闭' }}</b>
        <button class="btn btn-outline btn-sm" style="margin:0 8px" @click="toggleStyleRandom">{{ styleRandom ? '关闭' : '开启' }}</button>
        <span class="muted">不想挑风格时开启：每条任务随机选一种风格，批量任务篇篇不同；钉选的默认风格仍优先</span></p>
      <div v-if="styleSuggestion" class="card" style="margin:10px 0;border:1px solid #f59e0b">
        <p style="margin:0">你似乎偏好「{{ styleSuggestion.style_name }}」（{{ styleSuggestion.total }} 个任务，通过率 {{ fmtRate(styleSuggestion.approval_rate) }}）
          <button class="btn btn-primary btn-sm" style="margin-left:8px" @click="setDefaultStyle(styleSuggestion.style_name)">设为默认</button>
          <button class="btn btn-outline btn-sm" @click="prefillStyle(styleSuggestion.style_name)">存为我的风格</button>
        </p>
      </div>
      <table class="table" style="margin-top:10px" v-if="styleStats.length">
        <thead><tr><th>风格</th><th>任务数</th><th>通过数</th><th>通过率</th><th>重生成次数</th><th style="text-align:right">操作</th></tr></thead>
        <tbody>
          <tr v-for="s in styleStats" :key="s.style_name">
            <td><b>{{ s.style_name }}</b> <span v-if="s.style_name === styleDefault" class="tag tag-green">默认</span></td>
            <td>{{ s.total }}</td>
            <td>{{ s.approved }}</td>
            <td>{{ fmtRate(s.approval_rate) }}</td>
            <td>{{ s.regen_count }}</td>
            <td style="text-align:right">
              <button v-if="s.style_name !== styleDefault" class="btn btn-outline btn-sm" @click="setDefaultStyle(s.style_name)">设为默认</button>
              <button v-else class="btn btn-outline btn-sm" @click="clearDefaultStyle">取消默认</button>
              <button class="btn btn-outline btn-sm" @click="prefillStyle(s.style_name)">存为我的风格</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty" style="padding:14px 0">暂无统计数据——任务生图选定风格后自动累计</p>
    </div>

    <div class="card">
      <h2>风格关键词库 <span class="muted" style="font-weight:normal;font-size:13px">生图前按选题与正文自动匹配视觉风格：个人库优先，其次公共库，都空用系统内置 8 风格</span></h2>
      <div style="display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap">
        <input ref="styleCsv" type="file" accept=".csv" style="display:none" @change="importStyles">
        <button class="btn btn-outline btn-sm" @click="$refs.styleCsv.click()">📥 导入训练数据 CSV（style_name,keywords,description）</button>
        <input ref="styleLearnFiles" type="file" accept="image/*" multiple style="display:none" @change="learnStyle">
        <button class="btn btn-outline btn-sm" :disabled="styleLearnLoading" @click="$refs.styleLearnFiles.click()">{{ styleLearnLoading ? '分析中…（约需 1-2 分钟）' : '⤒ 从样例图学习（1-5 张）' }}</button>
        <a class="btn btn-outline btn-sm" style="text-decoration:none" href="/api/styles/template" download>下载模板</a>
        <label v-if="isAdmin" style="display:flex;align-items:center;gap:4px;font-size:13px">
          <input type="checkbox" v-model="styleImportPublic" style="width:auto"> 导入到公共库
        </label>
        <span v-if="styleImportMsg" class="muted" style="font-size:13px">{{ styleImportMsg }}</span>
        <span v-if="styleLearnMsg" class="muted" style="font-size:13px">{{ styleLearnMsg }}</span>
      </div>
      <form @submit.prevent="saveStyle" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input v-model="styleForm.style_name" placeholder="风格名（如：科技蓝调）" required style="flex:1">
        <input v-model="styleForm.keywords" placeholder="匹配关键词（逗号分隔，如：手机,数码,芯片,参数）" style="flex:2">
        <input v-model="styleForm.description" placeholder="视觉描述词（注入生图提示词）" style="flex:2">
        <input v-model="styleForm.variants" placeholder="变体轴（可选，分号分隔：每篇轮换的强调色/装饰，如 强调色用砖红；装饰用波点）" style="flex:2" title="变体轴：同一风格下每篇任务轮换采样的变体句，防空洞感；留空用内置变体">
        <label style="display:flex;align-items:center;gap:4px;font-size:14px;white-space:nowrap">
          <input type="checkbox" v-model="styleForm.enabled" style="width:auto"> 启用
        </label>
        <label v-if="isAdmin" style="display:flex;align-items:center;gap:4px;font-size:14px;white-space:nowrap">
          <input type="checkbox" v-model="styleForm.public" style="width:auto"> 公共条目
        </label>
        <button class="btn btn-primary btn-sm">{{ styleForm.id ? '更新' : '添加' }}</button>
        <button v-if="styleForm.id" type="button" class="btn btn-outline btn-sm" @click="styleForm=emptyStyleForm()">取消</button>
      </form>

      <h3 style="margin:16px 0 6px">我的风格</h3>
      <table class="table">
        <thead><tr><th>风格名</th><th>关键词</th><th>描述词</th><th>启用</th><th style="text-align:right">操作</th></tr></thead>
        <tbody>
          <tr v-for="r in myStyles" :key="r.id">
            <td><b>{{ r.style_name }}</b></td>
            <td class="muted" style="font-size:13px">{{ r.keywords || '—' }}</td>
            <td class="muted" style="font-size:13px">{{ r.description || '—' }}</td>
            <td><span class="tag" :class="r.enabled ? 'tag-green' : 'tag-gray'">{{ r.enabled ? '启用' : '停用' }}</span></td>
            <td style="text-align:right">
              <button class="btn btn-outline btn-sm" @click="editStyle(r)">编辑</button>
              <button class="btn btn-danger btn-sm" @click="removeStyle(r)">删除</button>
            </td>
          </tr>
          <tr v-if="!myStyles.length"><td colspan="5" class="muted" style="text-align:center;padding:14px">暂无个人条目——添加或导入训练数据后，生图时将优先按你的库匹配</td></tr>
        </tbody>
      </table>

      <h3 style="margin:16px 0 6px">公共风格 <span class="muted" style="font-weight:normal;font-size:13px">{{ isAdmin ? 'admin 可维护（表单勾选「公共条目」）' : '只读，由管理员维护' }}</span></h3>
      <table class="table">
        <thead><tr><th>风格名</th><th>关键词</th><th>描述词</th><th>启用</th><th style="text-align:right">操作</th></tr></thead>
        <tbody>
          <tr v-for="r in publicStyles" :key="r.id">
            <td><b>{{ r.style_name }}</b></td>
            <td class="muted" style="font-size:13px">{{ r.keywords || '—' }}</td>
            <td class="muted" style="font-size:13px">{{ r.description || '—' }}</td>
            <td><span class="tag" :class="r.enabled ? 'tag-green' : 'tag-gray'">{{ r.enabled ? '启用' : '停用' }}</span></td>
            <td style="text-align:right">
              <template v-if="isAdmin">
                <button class="btn btn-outline btn-sm" @click="editStyle(r)">编辑</button>
                <button class="btn btn-danger btn-sm" @click="removeStyle(r)">删除</button>
              </template>
              <span v-else class="muted" style="font-size:13px">只读</span>
            </td>
          </tr>
          <tr v-if="!publicStyles.length"><td colspan="5" class="muted" style="text-align:center;padding:14px">暂无公共条目</td></tr>
        </tbody>
      </table>
    </div>
    </template>

    <div class="card" v-if="tab==='logs'">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <h2 style="margin:0">{{ logsAdmin ? '工作日志（全部用户）' : '我的工作日志' }}</h2>
        <span style="margin-left:auto;display:flex;gap:8px;align-items:center">
          <select v-if="logsAdmin" v-model="logUser" @change="logPage=0; loadLogs()" style="width:auto">
            <option value="">全部用户</option>
            <option v-for="u in logUsers" :key="u" :value="u">{{ u }}</option>
          </select>
          <select v-model="logAction" @change="logPage=0; loadLogs()" style="width:auto">
            <option value="">全部动作</option>
            <option v-for="a in logActions" :key="a" :value="a">{{ actLabel(a) }}</option>
          </select>
          <button class="btn btn-outline btn-sm" @click="loadLogs">刷新</button>
        </span>
      </div>
      <table class="table" style="margin-top:12px" v-if="logs.length">
        <thead><tr><th class="th-sort" @click="toggleLogOrder" title="点击切换时间正序/倒序">时间{{ logOrder === 'asc' ? ' ▲' : ' ▼' }}</th><th v-if="logsAdmin">用户</th><th>动作</th><th>内容</th></tr></thead>
        <tbody>
          <tr v-for="l in logs" :key="l.id" style="cursor:default">
            <td class="muted" style="white-space:nowrap">{{ fmtTime(l.created_at) }}</td>
            <td v-if="logsAdmin">{{ l.actor_name }}</td>
            <td><span class="tag tag-blue">{{ actLabel(l.action) }}</span></td>
            <td style="white-space:pre-wrap">{{ l.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty" style="padding:20px 0">暂无日志</p>
      <div style="display:flex;align-items:center;gap:10px;margin-top:10px" v-if="logTotal > logLimit">
        <button class="btn btn-outline btn-sm" :disabled="logPage===0" @click="logPage--; loadLogs()">上一页</button>
        <span class="muted">{{ logPage + 1 }} / {{ logPages() }} 页 · 共 {{ logTotal }} 条</span>
        <button class="btn btn-outline btn-sm" :disabled="logPage + 1 >= logPages()" @click="logPage++; loadLogs()">下一页</button>
      </div>
    </div>

    <div class="card" style="max-width:520px" v-if="tab==='password'">
      <h2>修改我的密码</h2>
      <p class="muted">当前账号：{{ user.name }}（{{ user.role }}）</p>
      <form @submit.prevent="submitPw">
        <p><input v-model="old_password" type="password" required placeholder="原密码"></p>
        <p><input v-model="new_password" type="password" required placeholder="新密码（至少 8 位）"></p>
        <p><input v-model="confirm" type="password" required placeholder="再输入一次新密码"></p>
        <p v-if="pwError" class="form-error">{{ pwError }}</p>
        <p v-if="pwMsg" class="form-ok">{{ pwMsg }}</p>
        <button class="btn btn-primary" :disabled="savingPw">{{ savingPw ? '提交中…' : '保存新密码' }}</button>
      </form>
    </div>

    <template v-if="tab==='prompts'">
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="msg" class="form-ok">{{ msg }}</p>
    <div class="card">
      <div class="tabs">
        <button v-for="s in catalog" :key="s.stage" class="tab"
                :class="{on: curStage===s.stage}" @click="selectStage(s.stage)">{{ s.label }}</button>
      </div>
      <div v-if="modes.length > 1" class="tabs">
        <button v-for="m in modes" :key="m.mode" class="tab"
                :class="{on: curMode===m.mode}" @click="curMode=m.mode; form=null; sysForm=null">{{ m.mode_label }}模式</button>
      </div>
      <p class="muted" v-if="curStageObj">{{ curStageObj.hint }}</p>

      <div style="display:flex;align-items:center;gap:10px;margin-top:16px">
        <h2 style="margin:0">系统默认提示词</h2>
        <span v-if="curItem && curItem.customized" class="tag tag-yellow">已被管理员修改</span>
      </div>
      <p class="muted">{{ isAdmin ? '管理员可修改，修改后对所有用户生效。' : '仅管理员可修改；未启用自定义提示词时，你的任务使用此默认。' }}</p>
      <button class="btn btn-outline btn-sm" @click="showSystem=!showSystem">{{ showSystem ? '收起' : '查看' }}</button>
      <template v-if="isAdmin">
        <button class="btn btn-outline btn-sm" @click="startSysEdit" v-if="!sysForm">编辑</button>
        <button class="btn btn-outline btn-sm" @click="restoreSys" v-if="curItem && curItem.customized">恢复内置默认</button>
      </template>
      <pre v-if="showSystem && !sysForm" class="log-box" style="background:#f8f9fb;color:var(--text)">{{ curItem && curItem.system }}</pre>
      <div v-if="sysForm" style="margin-top:10px">
        <textarea v-model="sysForm.content" rows="12"></textarea>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn btn-primary btn-sm" @click="saveSys">保存为系统默认</button>
          <button class="btn btn-outline btn-sm" @click="sysForm=null">取消</button>
        </div>
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:20px">
        <h2 style="margin:0">自定义提示词{{ isAdmin ? '（全部用户）' : '（我的）' }}</h2>
        <button class="btn btn-primary btn-sm" @click="newPrompt" v-if="!form">新建（以系统默认起稿）</button>
      </div>
      <table class="table" v-if="filtered.length" style="margin-top:10px">
        <thead><tr>
          <th class="th-sort" @click="sortPrompts('name')">名称{{ pMark('name') }}</th><th v-if="isAdmin" class="th-sort" @click="sortPrompts('owner_name')">归属{{ pMark('owner_name') }}</th><th class="th-sort" @click="sortPrompts('is_active')">状态{{ pMark('is_active') }}</th><th class="th-sort" @click="sortPrompts('updated_at')">更新时间{{ pMark('updated_at') }}</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="p in filtered" :key="p.id">
            <td>{{ p.name }}</td>
            <td v-if="isAdmin">{{ p.owner_name }}</td>
            <td><span class="tag" :class="p.is_active ? 'tag-green' : 'tag-gray'">{{ p.is_active ? '已启用' : '未启用' }}</span></td>
            <td class="muted">{{ fmtTime(p.updated_at) }}</td>
            <td v-if="canEdit(p)">
              <button class="btn btn-outline btn-sm" @click.stop="editPrompt(p)">编辑</button>
              <button class="btn btn-outline btn-sm" @click.stop="toggle(p)">{{ p.is_active ? '停用' : '启用' }}</button>
              <button class="btn btn-danger btn-sm" @click.stop="del(p)">删除</button>
            </td>
            <td v-else class="muted">-</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty" style="padding:20px 0">暂无自定义提示词</p>
    </div>

    <div class="card" v-if="form">
      <h2>{{ form.id ? '编辑' : '新建' }}自定义提示词 · {{ curStageObj.label }}<template v-if="curItem && curItem.mode"> · {{ curItem.mode_label }}</template></h2>
      <p><input v-model="form.name" placeholder="提示词名称，如：活泼口吻版"></p>
      <p style="margin-top:10px"><textarea v-model="form.content" rows="12" placeholder="提示词内容"></textarea></p>
      <label style="display:flex;align-items:center;gap:6px;margin-top:10px;font-size:14px">
        <input type="checkbox" v-model="form.is_active" style="width:auto"> 保存后立即启用（同环节同模式的其它自定义提示词会自动停用）
      </label>
      <div style="display:flex;gap:8px;margin-top:14px">
        <button class="btn btn-primary" @click="save">保存</button>
        <button class="btn btn-outline" @click="form=null">取消</button>
      </div>
    </div>
    </template>
  </app-layout>`,
};
