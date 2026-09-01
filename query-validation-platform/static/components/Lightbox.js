// 图片全屏查看器（2026-08-27 重做）：点击配图整屏打开，显示大图 + 对应分页文案
// 多张切换：左右按钮、键盘 ←/→ 翻页、Esc 关闭
// 缩放：＋/− 按钮、滚轮、键盘 +/-，1:1 实际像素，0 恢复适应屏幕；放大后拖动平移
// ⛶ 按钮调用浏览器 Fullscreen API 进入真全屏
// 驳回标记（2026-08-31）：list 项带 markKey 时显示「标问题」条——看大图发现
// 细节问题可直接标记并填问题说明，emit toggle-mark / mark-reason 由父视图落状态
// 文案编辑（2026-09-01）：list 项带 editable 时文案区显示「✎ 改文案」——
// 保存时 emit save-text（payload 带 done 回调，父视图落库成功后才退出编辑态）
// 用法：<img-lightbox :img="zoom" @close="..." @toggle-mark="..." @mark-reason="..." @save-text="..." />
// zoom = {src, title, text}（单张）或 {list: [{src,title,text,markKey,marked,reason,editable}...], index: n}（多张）
const ImgLightbox = {
  props: { img: { type: Object, default: null } },
  emits: ['close', 'toggle-mark', 'mark-reason', 'save-text'],
  data() { return { idx: 0, scale: 0, natW: 0, natH: 0, drag: null,
                    editing: false, editText: '' }; },  // scale=0 表示适应屏幕
  computed: {
    list() {
      if (!this.img) return [];
      return this.img.list ? this.img.list : [this.img];
    },
    cur() {
      if (!this.list.length) return null;
      return this.list[Math.min(this.idx, this.list.length - 1)];
    },
    multi() { return this.list.length > 1; },
    scaleLabel() { return this.scale ? Math.round(this.scale * 100) + '%' : '适应'; },
    imgStyle() {
      if (!this.scale) return { maxWidth: '100%', maxHeight: '100%' };
      return { width: Math.round(this.natW * this.scale) + 'px',
               maxWidth: 'none', maxHeight: 'none' };
    },
  },
  watch: {
    idx() { this.editing = false; },  // 翻页时退出编辑态，防止把甲页文案存到乙页
    img(v, old) {
      // 列表签名：标记状态/说明文字变化不改变签名——避免父视图响应式重建
      // zoom（如在弹窗里填问题说明）时误重置翻页位置与缩放
      const sig = o => !o ? '' : (o.list
        ? o.list.length + '|' + ((o.list[0] || {}).src || '')
        : (o.src || ''));
      if (v) {
        if (!old) window.addEventListener('keydown', this.onKey);
        if (sig(v) !== sig(old) || (v.index || 0) !== ((old && old.index) || 0)) {
          this.idx = v.index || 0;
          this.scale = 0;
        }
      } else {
        window.removeEventListener('keydown', this.onKey);
      }
    },
  },
  beforeUnmount() { window.removeEventListener('keydown', this.onKey); },
  methods: {
    prev() { if (this.idx > 0) this.idx--; },
    next() { if (this.idx < this.list.length - 1) this.idx++; },
    onLoad(e) { this.natW = e.target.naturalWidth; this.natH = e.target.naturalHeight; },
    fitRatio() {
      const st = this.$refs.stage;
      if (!st || !this.natW || !this.natH) return 1;
      return Math.min(st.clientWidth / this.natW, st.clientHeight / this.natH);
    },
    zoomBtn(d) {
      const cur = this.scale || this.fitRatio();
      let next = Math.min(3, Math.max(0.15, cur * (d > 0 ? 1.25 : 0.8)));
      // 与适应比例接近时吸附回「适应」模式
      this.scale = Math.abs(next - this.fitRatio()) < 0.03 ? 0 : next;
    },
    actual() { this.scale = 1; },
    fit() { this.scale = 0; },
    onWheel(e) { this.zoomBtn(e.deltaY < 0 ? 1 : -1); },
    startDrag(e) {
      if (!this.scale) return;
      const st = this.$refs.stage;
      this.drag = { x: e.clientX, y: e.clientY, l: st.scrollLeft, t: st.scrollTop };
      e.preventDefault();
    },
    onDrag(e) {
      if (!this.drag) return;
      const st = this.$refs.stage;
      st.scrollLeft = this.drag.l - (e.clientX - this.drag.x);
      st.scrollTop = this.drag.t - (e.clientY - this.drag.y);
    },
    endDrag() { this.drag = null; },
    startEdit() { this.editText = this.cur.text || ''; this.editing = true; },
    saveEdit() {
      const t = (this.editText || '').trim();
      if (!t) return;
      // done 回调由父视图在落库后调用：成功才退出编辑态，失败保留内容继续改
      this.$emit('save-text', { item: this.cur, text: t,
                                done: ok => { if (ok) this.editing = false; } });
    },
    async toggleFs() {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await this.$refs.mask.requestFullscreen();
      } catch (e) { /* 浏览器拒绝全屏时静默降级为整屏遮罩 */ }
    },
    onKey(e) {
      if (e.key === 'Escape') this.$emit('close');
      else if (e.key === 'ArrowLeft') this.prev();
      else if (e.key === 'ArrowRight') this.next();
      else if (e.key === '+' || e.key === '=') this.zoomBtn(1);
      else if (e.key === '-') this.zoomBtn(-1);
      else if (e.key === '0') this.fit();
    },
  },
  template: `
  <div v-if="cur" class="lb-mask" ref="mask">
    <div class="lb-topbar">
      <b>{{ cur.title }}</b>
      <span v-if="multi" class="lb-counter">{{ idx + 1 }} / {{ list.length }}</span>
      <span class="lb-tools">
        <button title="缩小（-）" @click="zoomBtn(-1)">−</button>
        <span class="lb-scale" title="恢复适应屏幕（0）" @click="fit">{{ scaleLabel }}</span>
        <button title="放大（+）" @click="zoomBtn(1)">＋</button>
        <button title="实际像素" @click="actual">1:1</button>
        <button title="浏览器全屏" @click="toggleFs">⛶ 全屏</button>
        <button title="关闭（Esc）" @click="$emit('close')">✕</button>
      </span>
    </div>
    <div class="lb-stage" ref="stage" :class="{ zoomed: !!scale }"
         @wheel.prevent="onWheel" @mousedown="startDrag" @mousemove="onDrag"
         @mouseup="endDrag" @mouseleave="endDrag">
      <img class="lb-img" :src="cur.src" :key="cur.src" :style="imgStyle"
           @load="onLoad" draggable="false" alt="">
    </div>
    <div v-if="cur.text || cur.editable" class="lb-caption">
      <template v-if="!editing">
        <p>{{ cur.text }}</p>
        <button v-if="cur.editable" class="lb-editbtn" title="直接修改本页分页文案"
                @click="startEdit">✎ 改文案</button>
      </template>
      <template v-else>
        <textarea v-model="editText" class="lb-editarea" rows="3" maxlength="200"
                  placeholder="修改本页分页文案（保存后该页配图将按新文案重新生成）"></textarea>
        <div class="lb-editops">
          <button @click="editing = false">取消</button>
          <button class="primary" @click="saveEdit">保存文案</button>
        </div>
      </template>
    </div>
    <div v-if="cur.markKey" class="lb-markbar">
      <button class="lb-markbtn" :class="{on: cur.marked}"
              @click="$emit('toggle-mark', cur)">
        {{ cur.marked ? '✓ 已标记（点击取消）' : '⚑ 标问题' }}
      </button>
      <input v-if="cur.marked" class="lb-markreason" :value="cur.reason"
             @input="$emit('mark-reason', { markKey: cur.markKey, reason: $event.target.value })"
             placeholder="该图的问题说明（驳回/修正必填）">
    </div>
    <button v-if="multi" class="lb-nav lb-prev" :disabled="idx===0" @click="prev">‹</button>
    <button v-if="multi" class="lb-nav lb-next" :disabled="idx===list.length-1" @click="next">›</button>
  </div>`,
};
