// 回收站：已软删除的任务列表 + 恢复 / 彻底删除（后者需管理员密码）
const TrashView = {
  data() {
    return {
      list: [], total: 0, error: '', loading: false,
      page: 1, size: 20,
      selected: {},           // 勾选的任务 id -> true（批量恢复）
    };
  },
  computed: {
    actorName() { return (getUser() || {}).name || 'anonymous'; },
    isAdmin() { return (getUser() || {}).role === 'admin'; },
    pages() { return Math.max(1, Math.ceil(this.total / this.size)); },
    selectedIds() { return Object.keys(this.selected).filter(id => this.selected[id]); },
    allSelected() {
      return this.list.length > 0 && this.list.every(t => this.selected[t.id]);
    },
  },
  methods: {
    fmtTime,
    statusTag(s) { return STATUS[s] || { label: s, cls: 'tag-gray' }; },
    modeLabel(m) { return MODE[m] ? MODE[m].label : (m || '-'); },
    async load() {
      this.loading = true;
      try {
        const r = await api.get(`/api/trash?limit=${this.size}&offset=${(this.page - 1) * this.size}`);
        this.list = r.items; this.total = r.total; this.error = '';
        this.selected = {};
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    async restore(t) {
      if (!confirm(`确定恢复任务「${t.query}」？\n\n任务将回到移入前的状态（${this.statusTag(t.prev_status).label}），重新出现在任务中心。`)) return;
      try {
        await api.post(`/api/tasks/${t.id}/restore?actor=` + encodeURIComponent(this.actorName));
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    async purge(t) {
      if (!confirm(`确定彻底删除任务「${t.query}」？\n\n该任务及其全部生成内容（文案、配图、审核记录）将被永久清除，不可恢复！`)) return;
      let actor = this.actorName;
      if (!this.isAdmin) {
        const pwd = prompt('彻底删除需要管理员权限，请输入管理员密码：');
        if (pwd === null) return;
        try {
          await api.post('/api/auth/verify_admin', { password: pwd });
        } catch (e) { this.error = '管理员验证失败：' + e.message; return; }
        actor = 'admin';
      }
      try {
        await api.del(`/api/tasks/${t.id}/purge?actor=` + encodeURIComponent(actor));
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    toggleSelAll() {
      const on = !this.allSelected;
      const sel = {};
      if (on) this.list.forEach(t => { sel[t.id] = true; });
      this.selected = sel;
    },
    async restoreSelected() {
      const ids = this.selectedIds;
      if (!ids.length) return;
      if (!confirm(`确定恢复勾选的 ${ids.length} 条任务？\n\n任务将回到各自移入前的状态，重新出现在任务中心。`)) return;
      try {
        const r = await api.post('/api/tasks/restore_batch', { task_ids: ids, actor: this.actorName });
        let msg = `已恢复 ${r.restored} 条`;
        if (r.skipped) msg += `，跳过 ${r.skipped} 条`;
        alert(msg);
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    go(p) { if (p >= 1 && p <= this.pages) { this.page = p; this.load(); } },
  },
  mounted() { this.load(); },
  template: `
  <app-layout title="回收站">
    <p v-if="error" class="form-error">{{ error }}</p>
    <div class="card">
      <div style="margin-bottom:10px">
        <button class="btn btn-outline btn-sm" :disabled="!selectedIds.length" @click="restoreSelected">↩ 批量恢复（{{ selectedIds.length }}）</button>
      </div>
      <div v-if="!list.length && !loading" class="empty">回收站是空的</div>
      <table v-else class="table">
        <thead><tr><th style="width:32px"><input type="checkbox" style="width:auto" :checked="allSelected" @change="toggleSelAll" title="全选本页"></th><th>Query</th><th>模式</th><th>移入前状态</th><th>操作人</th><th>移入时间</th><th style="width:170px">操作</th></tr></thead>
        <tbody>
          <tr v-for="t in list" :key="t.id">
            <td><input type="checkbox" style="width:auto" v-model="selected[t.id]"></td>
            <td class="q-cell">{{ t.query }}</td>
            <td><span class="tag tag-blue">{{ modeLabel(t.mode) }}</span></td>
            <td><span class="tag" :class="statusTag(t.prev_status).cls">{{ statusTag(t.prev_status).label }}</span></td>
            <td class="muted">{{ t.trashed_by || '-' }}</td>
            <td class="muted">{{ fmtTime(t.trashed_at) }}</td>
            <td>
              <button class="btn btn-outline btn-sm" @click="restore(t)">↩ 恢复</button>
              <button class="btn btn-outline btn-sm" style="color:#c0392b;border-color:#c0392b" @click="purge(t)">彻底删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="pages > 1" style="margin-top:12px;text-align:right">
        <button class="btn btn-outline btn-sm" :disabled="page<=1" @click="go(page-1)">上一页</button>
        <span class="muted" style="margin:0 8px">{{ page }} / {{ pages }}</span>
        <button class="btn btn-outline btn-sm" :disabled="page>=pages" @click="go(page+1)">下一页</button>
      </div>
    </div>
    <p class="muted" style="margin-top:10px">移入回收站的任务已从任务中心隐藏，生成内容仍保留；「彻底删除」需管理员权限，删除后不可恢复。回收站不会自动清理。</p>
  </app-layout>`,
};
