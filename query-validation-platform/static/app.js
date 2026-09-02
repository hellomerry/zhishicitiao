// 应用入口：路由 + 全局组件注册
// 前端错误信标（2026-09-02）：把浏览器端报错上报到 /api/client_errors（容器日志可查）。
// Vue 生产版的组件渲染错误不进 window.onerror，必须靠 app.config.errorHandler 捕获。
function reportClientError(kind, msg, stack) {
  try {
    const u = (typeof getUser === 'function' && getUser()) || {};
    fetch('/api/client_errors', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, msg: String(msg || '').slice(0, 500),
        stack: String(stack || '').slice(0, 800), user: u.name || '', href: location.href }),
    }).catch(() => {});
  } catch (e) { /* 上报本身失败静默 */ }
}
window.addEventListener('error', e => reportClientError('onerror', e.message, e.error && e.error.stack));
window.addEventListener('unhandledrejection', e => reportClientError('rejection', e.reason && (e.reason.message || e.reason), e.reason && e.reason.stack));

const { createApp } = Vue;
const { createRouter, createWebHashHistory } = VueRouter;

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { public: true } },
    { path: '/', component: DashboardView },
    { path: '/import', component: ImportView },
    { path: '/tasks', component: TasksView },
    { path: '/monitor', component: MonitorView },
    { path: '/review', component: ReviewView },
    { path: '/sample', component: SampleView },
    { path: '/trash', component: TrashView },
    { path: '/settings', component: SettingsView },
    { path: '/prompts', redirect: '/settings' },
    { path: '/users', component: UsersView },
    { path: '/admin', component: AdminView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
});

router.beforeEach((to) => {
  if (!to.meta.public && !getUser()) return '/login';
});

const app = createApp({});
app.config.errorHandler = (err, instance, info) =>
  reportClientError('vue', err && (err.message || err), (err && err.stack ? err.stack + ' ' : '') + '[' + info + ']');
app.component('app-layout', AppLayout);
app.component('steps-bar', StepsBar);
app.component('img-lightbox', ImgLightbox);
app.use(router);
app.mount('#app');
