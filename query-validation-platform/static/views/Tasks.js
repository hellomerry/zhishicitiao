// 任务中心：列表（筛选/轮询/SSE 实时）+ 详情抽屉（13 节点进度 + 全部产物 + debug 日志）
const TasksView = {
  data() {
    return {
      list: [], total: 0, error: '', loading: false,
      approvedCount: 0,      // 审核通过的任务数（>0 才可导出内容包）
      fStatus: '', fMode: '', fRisk: '', auto: true,
      sort: 'created_at', order: 'desc',   // 列表排序（sort 白名单：created_at/status/mode）
      nodes: [], timer: null, es: null, sseTimer: null,
      live: {},              // task_id -> 内存实时态（current_node/debug/imgs）
      detail: null, detailError: '', retrying: false,
      uploadSubject: '',  // 手动上传参考图的标注（compare 模式 A:/B: 前缀）
      imageProvider: 'openai_images', savingModel: false,  // 任务级生图模型（默认 gpt-image-2）
      fixMode: false, fixMarks: {}, fixing: false,   // 创建者自助修正（定点标记+自动重生成）
      selected: {},           // 勾选的任务 id -> true（仅终态任务可勾选，用于批量移入回收站）
      exportJob: null, exportTimer: null,   // 任务式导出进度 {id,status,total,done,detail}
      showExport: false,     // 导出弹窗开关（关闭不中断后台打包）
      zoom: null,            // 图片放大浏览 {src, title, text}
    };
  },
  computed: {
    exportPct() {
      const j = this.exportJob;
      if (!j || !j.total) return 5;
      if (j.status === 'done') return 100;
      return Math.max(5, Math.round(j.done / j.total * 100));
    },
    nodeLabels() {
      const m = {}; this.nodes.forEach(n => { m[n.name] = n.label; }); return m;
    },
    detailTask() { return this.detail && this.detail.task; },
    actorName() { return (getUser() || {}).name || 'anonymous'; },
    liveOfDetail() {
      return this.detailTask ? (this.live[this.detailTask.id] || null) : null;
    },
    canRetry() {
      return this.detailTask && ['failed', 'rejected'].includes(this.detailTask.status);
    },
    canStartGen() {
      // 生图确认门：正文/分页文案/参考图已就绪，等人工确认后才执行生图（最贵步骤）
      return this.detailTask && this.detailTask.status === 'confirm_gen';
    },
    canFix() {
      // 创建者自助修正：终态任务（待审/已通过/已驳回）可定点标记问题项并自动重生成，
      // 不必等审核员驳回；归属隔离保证非 admin 只能看到/修正自己的任务
      return this.detailTask && ['review', 'approved', 'rejected'].includes(this.detailTask.status);
    },
    fixMarkList() {
      return Object.values(this.fixMarks)
        .sort((a, b) => a.page_index - b.page_index || (a.item_type > b.item_type ? 1 : -1));
    },
    canTrash() {
      return this.detailTask && ['review', 'approved', 'rejected', 'failed', 'confirm_gen'].includes(this.detailTask.status);
    },
    selectedIds() { return Object.keys(this.selected).filter(id => this.selected[id]); },
    allTrashableSelected() {
      const rows = this.list.filter(t => this.trashableRow(t));
      return rows.length > 0 && rows.every(t => this.selected[t.id]);
    },
    retryLabel() {
      if (!this.detailTask) return '';
      if (this.detailTask.status === 'rejected') {
        const n = ((this.detail && this.detail.reject_marks) || []).length;
        return n ? `↻ 定点重生成 ${n} 项（其余内容保留）` : '↻ 按驳回意见重新生产（全链重跑）';
      }
      return '↻ 重试该任务（跳过已完成节点）';
    },
    // 对比/单品任务：交付配图只展示正式版 AI 生成图，抓取的实景参考图单独成区
    genAssets() {
      return ((this.detail && this.detail.assets) || [])
        .filter(a => a.source_type !== 'official' && a.is_active !== false);
    },
    // 定点重生成被替换掉的历史版本（可对比、可换回正式）
    historyAssets() {
      return ((this.detail && this.detail.assets) || [])
        .filter(a => a.source_type !== 'official' && a.is_active === false);
    },
    refAssets() {
      return ((this.detail && this.detail.assets) || []).filter(a => a.source_type === 'official');
    },
  },
  methods: {
    fmtTime, roleName, riskReason,
    statusTag(s) { return STATUS[s] || { label: s, cls: 'tag-gray' }; },
    modeLabel(m) { return MODE[m] ? MODE[m].label : (m || '-'); },
    riskTag(r) { return RISK[r] || null; },
    nodeLabel(name) { return this.nodeLabels[name] || name || '-'; },
    buildQuery() {
      const p = new URLSearchParams();
      if (this.fStatus) p.set('status', this.fStatus);
      if (this.fMode) p.set('mode', this.fMode);
      if (this.fRisk) p.set('risk_level', this.fRisk);
      p.set('sort', this.sort);
      p.set('order', this.order);
      p.set('actor', this.actorName);
      p.set('limit', '100');
      return p.toString();
    },
    sortBy(col) {
      if (this.sort === col) {
        this.order = this.order === 'asc' ? 'desc' : 'asc';
      } else {
        this.sort = col;
        this.order = col === 'created_at' ? 'desc' : 'asc';
      }
      this.load();
    },
    sortMark(col) {
      if (this.sort !== col) return '';
      return this.order === 'asc' ? ' ▲' : ' ▼';
    },
    async load() {
      try {
        const [r, s] = await Promise.all([
          api.get('/api/tasks?' + this.buildQuery()),
          api.get('/api/tasks/stats?actor=' + encodeURIComponent(this.actorName)),
        ]);
        this.list = r.items; this.total = r.total; this.error = '';
        this.approvedCount = (s.by_status || {}).approved || 0;
        this.selected = {};
      } catch (e) { this.error = e.message; }
    },
    async loadLive() {
      try {
        const r = await api.get('/api/stream/state');
        const m = {};
        (r.tasks || []).forEach(t => { m[t.id] = t; });
        this.live = m;
      } catch (e) { /* 内存快照不可用时静默 */ }
    },
    async open(t) {
      this.detailError = ''; this.detail = null;
      this.fixMode = false; this.fixMarks = {}; this.fixing = false;
      try {
        this.detail = await api.get(`/api/tasks/${t.id}/detail?actor=` + encodeURIComponent(this.actorName));
        // 任务级生图模型（2026-08-28）：NULL/缺省 = 默认 gpt-image-2
        this.imageProvider = (this.detail.task && this.detail.task.image_provider) || 'openai_images';
      }
      catch (e) { this.detailError = e.message; }
    },
    async saveImageModel() {
      if (!this.detailTask) return;
      this.savingModel = true;
      try {
        await api.post(`/api/tasks/${this.detailTask.id}/image_model`,
          { actor: this.actorName, provider: this.imageProvider });
        this.detail.task.image_provider = this.imageProvider;
      } catch (e) { alert('设置生图模型失败：' + e.message); await this.open(this.detailTask); }
      finally { this.savingModel = false; }
    },
    fixKey(t, p) { return `${t}:${p}`; },
    isFixMarked(t, p) { return !!this.fixMarks[this.fixKey(t, p)]; },
    toggleFixMark(t, p) {
      const k = this.fixKey(t, p);
      const m = { ...this.fixMarks };
      if (m[k]) delete m[k];
      else m[k] = { item_type: t, page_index: p, reason: '' };
      this.fixMarks = m;
    },
    async submitFix() {
      if (!this.fixMarkList.length) { alert('请先在下方「分页文案/交付配图」里点「标问题」标记要修正的项'); return; }
      const bad = this.fixMarkList.find(m => !m.reason.trim());
      if (bad) { alert(`请填写 ${bad.item_type === 'page' ? '文案' : '配图'}P${bad.page_index} 的问题说明`); return; }
      if (!confirm(`确定提交修正？\n\n仅自动重生成被标记的 ${this.fixMarkList.length} 项（其余已认可内容保留），重生成后任务回到审核队列终审。`)) return;
      this.fixing = true;
      try {
        await api.post(`/api/tasks/${this.detailTask.id}/fix`, { actor: this.actorName, marks: this.fixMarkList });
        this.fixMode = false; this.fixMarks = {};
        await this.load();
        await this.open(this.detailTask);
      } catch (e) { alert('提交失败：' + e.message); }
      finally { this.fixing = false; }
    },
    close() { this.detail = null; },
    async saveAsMyStyle() {
      // 存为我的风格（2026-08-28）：反查风格描述词后预填进设置页风格库表单
      const t = this.detailTask || {};
      let desc = '';
      try {
        const r = await api.get('/api/styles/lookup?style_name=' + encodeURIComponent(t.gen_image_style)
          + '&actor=' + encodeURIComponent(this.actorName));
        desc = r.description || '';
      } catch (e) { /* 反查失败也照常跳转，描述词留空 */ }
      localStorage.setItem('qvp_style_prefill', JSON.stringify(
        { style_name: t.gen_image_style || '', keywords: '', description: desc }));
      location.hash = '#/settings';
    },
    pageCopyOf(i) {
      const pcs = (this.detail && this.detail.page_copies) || [];
      const p = pcs.find(x => x.page_index === i);
      return p ? p.body : '';
    },
    zoomItems() {
      const gen = this.genAssets.map(a => ({
        src: a.display_url || a.image_url,
        title: `P${a.page_index} · ${a.version_no > 1 ? `修正版 第${a.version_no}版` : '初版'}`,
        text: this.pageCopyOf(a.page_index),
      }));
      const hist = this.historyAssets.map(a => ({
        src: a.display_url || a.image_url,
        title: `P${a.page_index} · 历史 第${a.version_no}版`,
        text: this.pageCopyOf(a.page_index),
      }));
      const ref = this.refAssets.map(a => ({
        src: a.display_url || a.image_url,
        title: `参考 ${a.page_index} · 实景抓取`,
        text: '',
      }));
      return gen.concat(hist, ref);
    },
    async activateAsset(a) {
      if (!confirm(`把 P${a.page_index} 的这个历史版本设为正式版？当前正式版将转为历史版本（可随时再换回），交叉校验与风险分级会按新版重建。`)) return;
      try {
        await api.post(`/api/assets/${a.id}/activate?actor=` + encodeURIComponent(this.actorName));
        await this.open(this.detailTask);
      } catch (e) { alert('操作失败：' + e.message); }
    },
    async delRef(a) {
      if (!confirm(`删除这张参考图（参考 ${a.page_index}）？删除后不可恢复。`)) return;
      try {
        await api.del(`/api/assets/${a.id}/ref?actor=` + encodeURIComponent(this.actorName));
        await this.open(this.detailTask);
      } catch (e) { alert('删除失败：' + e.message); }
    },
    // 实图角标（2026-08-28 用户要求实图带标识）：搜索标的配色 + 来源标签
    refSubjectClass(a) {
      const s = a.subject || '';
      if (s.startsWith('A:')) return 'tag-blue';
      if (s.startsWith('B:')) return 'tag-green';
      return 'tag-gray';
    },
    refSource(a) {
      if (a.engine === 'upload') return '上传';
      if ((a.subject || '').includes('补搜')) return '补搜';
      return '搜索';
    },
    async researchRefs() {
      const t = this.detailTask || {};
      const def = (t.query || '') + ' 高清';
      const q = prompt('输入参考图搜索词（搜 20 张，达最低分辨率的全保留并自动排重）：', def);
      if (q === null || !q.trim()) return;
      const body = { actor: this.actorName, query: q.trim() };
      if (t.mode === 'compare') {
        // 生图按 A:/B: 前缀分配参考图池；标注不带前缀会进公共池、每页都喂
        const tags = [...new Set(this.refAssets
          .map(a => a.subject).filter(s => s && /^(A|B):/.test(s)))];
        const hint = tags.length
          ? tags.map((s, i) => `${i + 1}. ${s}`).join('\n')
          : '（当前无 A:/B: 标注）';
        const pick = prompt(
          '对比模式需标注搜索标的（生图按 A:/B: 前缀分配参考图池）：\n' + hint +
          '\n\n输入编号选择，或直接输入标注（如 A:小米17 Pro（细节））：',
          tags[0] || 'A:');
        if (pick === null || !pick.trim()) return;
        const idx = parseInt(pick.trim(), 10);
        body.subject = (idx >= 1 && idx <= tags.length) ? tags[idx - 1] : pick.trim();
      }
      try {
        const r = await api.post(`/api/tasks/${t.id}/ref_search`, body);
        alert(`已追加 ${r.added} 张参考图` + (r.filtered ? `（过滤低质 ${r.filtered} 张）` : '') +
          (r.dupes ? `（重复跳过 ${r.dupes} 张）` : '') +
          (body.subject ? `，标注「${body.subject}」` : ''));
        await this.open(this.detailTask);
      } catch (e) { alert('重搜失败：' + e.message); }
    },
    triggerRefUpload() {
      // compare 模式先问标注（生图按 A:/B: 前缀分配参考图池），再打开文件选择
      const t = this.detailTask || {};
      this.uploadSubject = '';
      if (t.mode === 'compare') {
        const tags = [...new Set(this.refAssets
          .map(a => a.subject).filter(s => s && /^(A|B):/.test(s)))];
        const hint = tags.length
          ? tags.map((s, i) => `${i + 1}. ${s}`).join('\n')
          : '（当前无 A:/B: 标注）';
        const pick = prompt(
          '对比模式需标注上传图片的标的（生图按 A:/B: 前缀分配参考图池）：\n' + hint +
          '\n\n输入编号选择，或直接输入标注（如 A:小米17 Pro）：',
          tags[0] || 'A:');
        if (pick === null || !pick.trim()) return;
        const idx = parseInt(pick.trim(), 10);
        this.uploadSubject = (idx >= 1 && idx <= tags.length) ? tags[idx - 1] : pick.trim();
      }
      this.$refs.refFileInput.value = '';
      this.$refs.refFileInput.click();
    },
    async uploadRefs(e) {
      const files = Array.from(e.target.files || []);
      if (!files.length || !this.detailTask) return;
      const fd = new FormData();
      fd.append('actor', this.actorName);
      fd.append('subject', this.uploadSubject || '');
      files.forEach(f => fd.append('files', f));
      try {
        const r = await api.postForm(`/api/tasks/${this.detailTask.id}/ref_upload`, fd);
        alert(`已上传 ${r.added} 张参考图` +
          (r.dupes ? `（重复跳过 ${r.dupes} 张）` : '') +
          (r.rejected ? `（非图片/超限拒收 ${r.rejected} 张）` : '') +
          (r.subject ? `，标注「${r.subject}」` : ''));
        await this.open(this.detailTask);
      } catch (err) { alert('上传失败：' + err.message); }
    },
    openZoom(a, isRef) {
      const list = this.zoomItems();
      const src = a.display_url || a.image_url;
      const index = Math.max(0, list.findIndex(x => x.src === src));
      this.zoom = { list, index };
    },
    async retry() {
      if (!this.detailTask) return;
      const marks = (this.detail && this.detail.reject_marks) || [];
      let msg;
      if (this.detailTask.status === 'rejected' && marks.length) {
        msg = `确定定点重生成该任务？\n\n仅重做被标记的 ${marks.length} 项（${marks.map(m => (m.item_type === 'page' ? '文案' : '配图') + 'P' + m.page_index).join('、')}），其余已认可内容保留不重跑，只产生标记项的生成费用。`;
      } else if (this.detailTask.status === 'rejected') {
        msg = '确定重新生产该任务？\n\n上一轮生成的文案和配图将被清除，审核驳回理由会注入提示词重新生成全部内容（会产生完整的生成费用）。';
      } else {
        msg = '确定重试该任务？已完成的节点会自动跳过。';
      }
      if (!confirm(msg)) return;
      this.retrying = true;
      try {
        await api.post(`/api/tasks/${this.detailTask.id}/retry?actor=` + encodeURIComponent((getUser() || {}).name || 'anonymous'));
        await this.load();
        await this.open(this.detailTask);
      } catch (e) { this.detailError = e.message; }
      finally { this.retrying = false; }
    },
    async startGen() {
      if (!this.detailTask) return;
      // 实图保底 6 张（2026-08-27）：不足时提示先重搜/上传补足，不强制拦截
      const warn = (this.detailTask.mode !== 'general' && this.refAssets.length < 6)
        ? `⚠ 当前参考图仅 ${this.refAssets.length} 张（少于 6 张），建议先「重搜/上传参考图」补足。\n\n` : '';
      const modelName = this.imageProvider === 'gemini' ? 'Gemini 3 Pro Image' : 'gpt-image-2（默认）';
      if (!confirm(warn + `确认正文、分页文案与参考图无误，使用「${modelName}」开始生图？\n\n生图是最贵步骤（每任务 6 张）。确认前可先重搜/上传/删除参考图；放行后自动完成生图、OCR、校验并进入审核。`)) return;
      this.retrying = true;
      try {
        await api.post(`/api/tasks/${this.detailTask.id}/start_gen?actor=` + encodeURIComponent(this.actorName));
        await this.load();
        await this.open(this.detailTask);
      } catch (e) { this.detailError = e.message; }
      finally { this.retrying = false; }
    },
    async trashTask() {
      if (!this.detailTask) return;
      if (!confirm('确定把该任务移入回收站？\n\n任务将从任务中心隐藏，生成的文案和配图暂时保留；可在「回收站」中恢复，或由管理员彻底删除。')) return;
      try {
        await api.post(`/api/tasks/${this.detailTask.id}/trash?actor=` + encodeURIComponent(this.actorName));
        this.close();
        await this.load();
      } catch (e) { this.detailError = e.message; }
    },
    trashableRow(t) { return ['review', 'approved', 'rejected', 'failed', 'confirm_gen'].includes(t.status); },
    toggleSelAll() {
      const rows = this.list.filter(t => this.trashableRow(t));
      const on = !this.allTrashableSelected;
      const sel = {};
      if (on) rows.forEach(t => { sel[t.id] = true; });
      this.selected = sel;
    },
    async trashSelected() {
      const ids = this.selectedIds;
      if (!ids.length) return;
      if (!confirm(`确定把勾选的 ${ids.length} 条任务移入回收站？\n\n排队/生产中的任务不可移入；移入后可在「回收站」中恢复。`)) return;
      try {
        const r = await api.post('/api/tasks/trash_batch', { task_ids: ids, actor: this.actorName });
        let msg = `已移入回收站 ${r.moved} 条`;
        if (r.skipped) msg += `，跳过 ${r.skipped} 条（非终态）`;
        alert(msg);
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    onSse() {
      // 任意任务/节点事件 → 节流刷新列表与打开的详情
      if (this.sseTimer) return;
      this.sseTimer = setTimeout(() => {
        this.sseTimer = null;
        this.load(); this.loadLive();
        if (this.detailTask) this.open(this.detailTask);
      }, 800);
    },
    async startExport() {
      this.error = '';
      try {
        const r = await api.post('/api/export/approved/start?actor=' + encodeURIComponent(this.actorName));
        this.exportJob = { id: r.job_id, status: 'running', total: r.total, done: 0, detail: '启动打包…' };
        this.showExport = true;
        this.exportTimer = setInterval(this.pollExport, 1000);
      } catch (e) { this.error = e.message; }
    },
    async pollExport() {
      if (!this.exportJob) return;
      try {
        const s = await api.get('/api/export/' + this.exportJob.id);
        Object.assign(this.exportJob, s);
        if (s.status === 'done') {
          // 打包完成：展示分包下载按钮，用户逐包下载（不自动触发）
          // 已导出任务已被自动移入回收站，刷新列表让任务中心同步
          clearInterval(this.exportTimer); this.exportTimer = null;
          this.load();
        } else if (s.status === 'error') {
          clearInterval(this.exportTimer); this.exportTimer = null;
          this.error = '导出失败：' + (s.error || '未知错误');
          this.exportJob = null;
          this.showExport = false;
        }
      } catch (e) { /* 单次轮询失败静默，下轮重试 */ }
    },
    fmtSize(b) {
      if (!b) return '0B';
      return b >= 1048576 ? (b / 1048576).toFixed(1) + 'MB' : Math.round(b / 1024) + 'KB';
    },
  },
  watch: {
    fStatus() { this.load(); }, fMode() { this.load(); }, fRisk() { this.load(); },
  },
  async mounted() {
    this.load(); this.loadLive();
    try { this.nodes = (await api.get('/api/meta/nodes')).nodes; } catch (e) { /* 降级显示英文名 */ }
    this.timer = setInterval(() => { if (this.auto) { this.load(); this.loadLive(); } }, 5000);
    this.es = new EventSource('/api/stream/events');
    this.es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type && (d.type.startsWith('task_') || d.type.startsWith('node_'))) this.onSse();
      } catch (e) { /* ping 等非 JSON 帧忽略 */ }
    };
  },
  beforeUnmount() {
    clearInterval(this.timer);
    clearInterval(this.exportTimer);
    clearTimeout(this.sseTimer);
    if (this.es) this.es.close();
  },
  template: `
  <app-layout title="任务中心">
    <div class="card filter-bar">
      <select v-model="fStatus">
        <option value="">全部状态</option>
        <option v-for="(s, k) in STATUS" :key="k" :value="k">{{ s.label }}</option>
      </select>
      <select v-model="fMode">
        <option value="">全部模式</option>
        <option v-for="(m, k) in MODE" :key="k" :value="k">{{ m.label }}</option>
      </select>
      <select v-model="fRisk">
        <option value="">全部风险</option>
        <option value="green">绿</option><option value="yellow">黄</option><option value="red">红</option>
      </select>
      <label class="auto-refresh"><input type="checkbox" v-model="auto" style="width:auto"> 自动刷新</label>
      <template v-if="approvedCount > 0 || exportJob">
        <button v-if="!exportJob" class="btn btn-outline btn-sm" @click="startExport">📦 导出已通过内容包（{{ approvedCount }}）</button>
        <button v-else-if="exportJob.status !== 'done'" class="btn btn-outline btn-sm" @click="showExport = true">📦 打包中… {{ exportPct }}%</button>
        <button v-else class="btn btn-primary btn-sm" @click="showExport = true">📦 下载内容包（{{ (exportJob.parts || []).length }} 包）</button>
      </template>
      <span v-else class="btn btn-outline btn-sm" style="opacity:.55;cursor:not-allowed" title="任务经审核角色（A/B/C 任一）审核通过后，即进入导出通道">📦 导出已通过内容包（0）</span>
      <button class="btn btn-outline btn-sm" :disabled="!selectedIds.length" @click="trashSelected">🗑 移入回收站（{{ selectedIds.length }}）</button>
      <span class="muted" style="margin-left:auto">共 {{ total }} 条</span>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <div class="card">
      <div v-if="!list.length" class="empty">暂无任务，<router-link to="/import">去导入 →</router-link></div>
      <table v-else class="table">
        <thead><tr><th style="width:32px"><input type="checkbox" style="width:auto" :checked="allTrashableSelected" @change="toggleSelAll" title="全选可移入回收站的任务"></th><th>Query</th><th class="th-sort" @click="sortBy('mode')">模式{{ sortMark('mode') }}</th><th class="th-sort" @click="sortBy('status')">状态{{ sortMark('status') }}</th><th>风险</th><th>当前节点</th><th class="th-sort" @click="sortBy('created_at')">创建时间{{ sortMark('created_at') }}</th></tr></thead>
        <tbody>
          <tr v-for="t in list" :key="t.id" @click="open(t)" :class="{selected: detailTask && detailTask.id === t.id}">
            <td @click.stop><input v-if="trashableRow(t)" type="checkbox" style="width:auto" v-model="selected[t.id]"></td>
            <td class="q-cell">{{ t.query }}</td>
            <td><span class="tag tag-blue">{{ modeLabel(t.mode) }}</span></td>
            <td><span class="tag" :class="statusTag(t.status).cls">{{ statusTag(t.status).label }}</span><span v-if="t.open_marks && ['draft','processing'].includes(t.status)" class="tag tag-yellow" :title="'审核驳回/自助修正已提交，系统正在自动重跑 '+t.open_marks+' 项标记，完成后自动回审核'">🔄 修正中×{{ t.open_marks }}</span></td>
            <td><span v-if="riskTag(t.risk_level)" class="tag" :class="riskTag(t.risk_level).cls">{{ riskTag(t.risk_level).label }}</span><span v-else class="muted">-</span></td>
            <td>{{ nodeLabel(t.current_node) }}</td>
            <td class="muted">{{ fmtTime(t.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="detail || detailError" class="drawer-mask" @click.self="close">
      <div class="drawer">
        <p v-if="detailError" class="form-error">{{ detailError }}</p>
        <template v-if="detail">
          <div class="drawer-head">
            <h2>{{ detailTask.query }}</h2>
            <button class="btn btn-outline" @click="close">关闭</button>
          </div>
          <p>
            <span class="tag" :class="statusTag(detailTask.status).cls">{{ statusTag(detailTask.status).label }}</span>
            <span class="tag tag-blue">{{ modeLabel(detailTask.mode) }}</span>
            <span v-if="detailTask.gen_image_style" class="tag tag-green" title="生图前按选题与正文自动判定的视觉风格，6 张图共用">图片风格：{{ detailTask.gen_image_style }}</span>
            <button v-if="detailTask.gen_image_style" class="btn btn-outline btn-sm" style="margin-left:6px"
                    title="把该风格名+描述词预填进设置页风格库表单，补匹配关键词后保存到个人库"
                    @click="saveAsMyStyle">存为我的风格</button>
            <span v-if="detail.risk" class="tag" :class="riskTag(detail.risk.level).cls">风险：{{ riskTag(detail.risk.level).label }}</span>
            <span class="muted" style="margin-left:8px">{{ fmtTime(detailTask.created_at) }}</span>
          </p>

          <h3>生产进度</h3>
          <steps-bar :nodes="nodes" :completed="detail.completed_nodes" :current="detailTask.status === 'failed' ? detail.current_node : (liveOfDetail && liveOfDetail.current_node) || detail.current_node" :failed="detailTask.status === 'failed'"></steps-bar>

          <div v-if="canRetry || canTrash || canFix || canStartGen" style="margin:12px 0">
            <button v-if="canStartGen" class="btn btn-primary" :disabled="retrying" @click="startGen">{{ retrying ? '处理中…' : '▶ 确认并开始生图' }}</button>
            <button v-if="canRetry" class="btn btn-primary" :disabled="retrying" @click="retry">{{ retrying ? '处理中…' : retryLabel }}</button>
            <button v-if="canFix" class="btn btn-outline" @click="fixMode = !fixMode; fixMarks = {}">{{ fixMode ? '取消修正' : '✎ 标记修正' }}</button>
            <button v-if="canTrash" class="btn btn-outline" @click="trashTask">🗑 移入回收站</button>
          </div>

          <div v-if="canStartGen" class="card" style="margin:12px 0;border:1px solid #f59e0b">
            <p style="margin:0"><b>待生图确认</b>：<span class="muted">正文、分页文案与实景参考图已就绪。请检查下方内容：参考图可「重搜/上传/删除」调整；确认无误后点「▶ 确认并开始生图」，系统将生成 6 张配图并完成校验进入审核。</span></p>
            <p style="margin:8px 0 0"><b>生图模型</b>：
              <select v-model="imageProvider" @change="saveImageModel" :disabled="savingModel" style="margin:0 6px">
                <option value="openai_images">gpt-image-2（默认，约 ¥0.2/张）</option>
                <option value="gemini">Gemini 3 Pro Image（写实风，约 ¥0.6/张）</option>
              </select>
              <span class="muted" style="font-size:12px">默认 gpt-image-2；手动选择其它模型仅对本任务生效</span>
            </p>
            <p v-if="detailTask.mode !== 'general' && refAssets.length < 6" style="margin:6px 0 0;color:#c00">⚠ 参考图仅 {{ refAssets.length }} 张（少于保底 6 张），建议先「重搜/上传参考图」补足再生图。</p>
          </div>

          <div v-if="fixMode" class="card" style="margin:12px 0;border:1px solid #f59e0b">
            <p style="margin:0 0 8px"><b>自助修正</b>：<span class="muted">在下方「分页文案 / 交付配图」中点「⚑ 标问题」标记有问题的项并填写说明，提交后仅重生成标记项（其余内容保留），完成后任务回到审核队列终审。</span></p>
            <template v-if="fixMarkList.length">
              <div v-for="m in fixMarkList" :key="m.item_type + m.page_index" style="display:flex;align-items:center;gap:8px;margin:6px 0">
                <span class="tag tag-yellow" style="white-space:nowrap">{{ m.item_type === 'page' ? '文案' : '配图' }} P{{ m.page_index }}</span>
                <input v-model="m.reason" placeholder="该项的问题说明（必填）" style="flex:1">
                <button class="btn btn-outline btn-sm" @click="toggleFixMark(m.item_type, m.page_index)">移除</button>
              </div>
            </template>
            <p v-else class="muted" style="margin:6px 0">尚未标记任何项。</p>
            <button class="btn btn-primary" style="margin-top:8px" :disabled="fixing" @click="submitFix">{{ fixing ? '提交中…' : '提交修正（自动重生成 ' + fixMarkList.length + ' 项）' }}</button>
          </div>

          <template v-if="detail.reject_marks && detail.reject_marks.length">
            <h3>定点驳回标记（重试时仅重生成这些项）</h3>
            <ul class="plain-list">
              <li v-for="m in detail.reject_marks" :key="m.item_type + m.page_index">
                <span class="tag tag-yellow">{{ m.item_type === 'page' ? '文案' : '配图' }} P{{ m.page_index }}</span> {{ m.reason }}
              </li>
            </ul>
          </template>

          <template v-if="detail.risk && detail.risk.reasons && detail.risk.reasons.length">
            <h3>风险原因</h3>
            <ul class="plain-list"><li v-for="r in detail.risk.reasons" :key="r">{{ riskReason(r) }}</li></ul>
          </template>

          <template v-if="detail.draft">
            <h3>正文（{{ detail.draft.model_version }}）</h3>
            <p class="article-body">{{ detail.draft.body }}</p>
          </template>

          <template v-if="detail.page_copies && detail.page_copies.length">
            <h3>分页文案<span v-if="fixMode" class="muted" style="font-weight:normal;font-size:13px">（点「标问题」标记要修正的页）</span></h3>
            <div v-for="p in detail.page_copies" :key="p.page_index" class="page-copy" :style="fixMode ? 'display:flex;align-items:flex-start;gap:10px' : ''">
              <b>P{{ p.page_index }}</b>
              <p class="muted" :style="fixMode ? 'flex:1;margin:0' : ''">{{ p.body }}</p>
              <button v-if="fixMode" class="btn btn-sm" :class="isFixMarked('page', p.page_index) ? 'btn-danger' : 'btn-outline'"
                      @click="toggleFixMark('page', p.page_index)">
                {{ isFixMarked('page', p.page_index) ? '✓ 已标记' : '⚑ 标问题' }}
              </button>
            </div>
          </template>

          <template v-if="genAssets.length">
            <h3>交付配图（{{ genAssets.length }}）<span v-if="fixMode" class="muted" style="font-weight:normal;font-size:13px">（点「标问题」标记要修正的图）</span></h3>
            <div class="img-grid">
              <figure v-for="a in genAssets" :key="a.id">
                <img :src="a.display_url || a.image_url" loading="lazy" alt="" @click="openZoom(a, false)">
                <figcaption class="muted">P{{ a.page_index }} · <span v-if="a.version_no > 1" class="tag tag-blue">修正版 第{{ a.version_no }}版</span><span v-else>初版</span>
                  <button v-if="fixMode" class="btn btn-sm" :class="isFixMarked('image', a.page_index) ? 'btn-danger' : 'btn-outline'"
                          style="margin-left:6px" @click.stop="toggleFixMark('image', a.page_index)">
                    {{ isFixMarked('image', a.page_index) ? '✓ 已标记' : '⚑ 标问题' }}
                  </button>
                </figcaption>
              </figure>
            </div>
          </template>

          <template v-if="historyAssets.length">
            <h3>历史版本配图（{{ historyAssets.length }}）<span class="muted" style="font-weight:normal;font-size:13px">定点重生成被替换的旧版，可对比后换回正式</span></h3>
            <div class="img-grid">
              <figure v-for="a in historyAssets" :key="a.id">
                <img :src="a.display_url || a.image_url" loading="lazy" alt="" @click="openZoom(a, false)">
                <figcaption class="muted">P{{ a.page_index }} · 历史 第{{ a.version_no }}版
                  <a href="javascript:;" style="color:#06c;margin-left:6px" title="把此版本设为正式版" @click="activateAsset(a)">设为正式</a>
                  <div v-if="a.reject_reasons && a.reject_reasons.length" style="font-size:12px;color:#d97706;line-height:1.5;white-space:normal" title="该版本被驳回修改的原因">驳回：{{ a.reject_reasons.join('；') }}</div>
                </figcaption>
              </figure>
            </div>
          </template>

          <h3>实景参考图（{{ refAssets.length }}）<span class="muted" style="font-weight:normal;font-size:13px">仅作生图参考，不随内容交付</span>
            <button class="btn btn-outline btn-sm" style="margin-left:10px" @click="researchRefs">↻ 重搜参考图</button>
            <button class="btn btn-outline btn-sm" style="margin-left:6px" @click="triggerRefUpload">⤒ 上传参考图</button>
            <input ref="refFileInput" type="file" accept="image/*" multiple style="display:none" @change="uploadRefs"></h3>
          <div class="img-grid" v-if="refAssets.length">
            <figure v-for="a in refAssets" :key="a.id">
              <img :src="a.display_url || a.image_url" loading="lazy" alt="" @click="openZoom(a, true)">
              <div class="ref-badges">
                <span class="tag" :class="refSubjectClass(a)" :title="a.search_query ? '搜索词：' + a.search_query : ''">{{ a.subject || '公共池' }}</span>
                <span class="tag tag-gray">{{ refSource(a) }}</span>
              </div>
              <figcaption class="muted">参考 {{ a.page_index }} · {{ a.subject || '实景抓取' }}<template v-if="a.search_query"><br>词：{{ a.search_query }}</template>
                <a href="javascript:;" style="color:#c00;margin-left:6px" title="删除此参考图" @click="delRef(a)">删除</a></figcaption>
            </figure>
          </div>
          <p v-else class="muted">暂无参考图，可点「重搜参考图」搜索或「上传参考图」手动上传。</p>

          <template v-if="detail.claims && detail.claims.length">
            <h3>事实点</h3>
            <ul class="plain-list"><li v-for="c in detail.claims" :key="c.claim_text">{{ c.claim_text }} <span class="tag tag-gray">{{ c.risk_level }}</span></li></ul>
          </template>

          <template v-if="detail.evidences && detail.evidences.length">
            <h3>证据来源</h3>
            <ul class="plain-list"><li v-for="e in detail.evidences" :key="e.source_url"><a :href="e.source_url" target="_blank">{{ e.source_url }}</a></li></ul>
          </template>

          <h3>审核进度</h3>
          <div class="review-progress">
            <div v-for="r in detail.review" :key="r.role" class="review-cell">
              <span class="tag tag-blue">{{ r.role }} · {{ roleName(r.role) }}</span>
              <span v-if="r.action === 'approve'" class="tag tag-green">已通过</span>
              <span v-else-if="r.action === 'reject'" class="tag tag-red">已驳回</span>
              <span v-else class="tag tag-gray">待审</span>
              <span v-if="r.reviewer" class="muted">{{ r.reviewer }}</span>
            </div>
          </div>

          <template v-if="liveOfDetail && liveOfDetail.debug && liveOfDetail.debug.length">
            <h3>运行日志（本次运行）</h3>
            <pre class="log-box">{{ liveOfDetail.debug.map(d => '[' + d.node + '] ' + d.phase + ' ' + (d.elapsed || '') + 's ' + (d.msg || '') + (d.trace ? '\\n' + d.trace : '')).join('\\n') }}</pre>
          </template>
        </template>
        <div v-else-if="!detailError" class="empty">加载中…</div>
      </div>
    </div>
    <div v-if="showExport && exportJob" class="drawer-mask" @click.self="showExport = false">
      <div class="export-modal">
        <div class="drawer-head">
          <h2>📦 导出已通过内容包</h2>
          <button class="btn btn-outline btn-sm" @click="showExport = false">关闭</button>
        </div>
        <template v-if="exportJob.status !== 'done'">
          <div class="export-progress-row">
            <div class="bar-track"><div class="bar-fill" :style="{width: exportPct + '%'}"></div></div>
            <span class="bar-count">{{ exportPct }}%</span>
          </div>
          <p class="muted export-detail">{{ exportJob.detail }}（{{ exportJob.done }}/{{ exportJob.total }}）</p>
          <p class="muted">打包在服务器后台进行，关闭本窗口不会中断，可稍后再点开查看。</p>
        </template>
        <template v-else>
          <p class="export-detail">{{ exportJob.detail }}</p>
          <div class="export-parts">
            <a v-for="p in exportJob.parts" :key="p.part" class="btn btn-outline btn-sm"
               :href="'/api/export/' + exportJob.id + '/download/' + p.part" style="text-decoration:none">⬇ 第{{ p.part }}包（{{ p.tasks }}条 · {{ fmtSize(p.size) }}）</a>
          </div>
          <p class="muted">每 10 条打成一个 zip，逐包点击下载；下载进度由浏览器管理。内容包在服务器保留约 1 小时。</p>
          <p class="muted">已导出的任务已自动移入「回收站」（任务中心不再显示），如导出的内容有问题可在回收站恢复。</p>
          <div style="text-align:right;margin-top:14px">
            <button class="btn btn-primary btn-sm" @click="exportJob = null; showExport = false">完成</button>
          </div>
        </template>
      </div>
    </div>
    <img-lightbox :img="zoom" @close="zoom=null" />
  </app-layout>`,
  created() { this.STATUS = STATUS; this.MODE = MODE; },
};
