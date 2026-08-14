// 共享：登录状态 + 侧边栏菜单 + 顶部栏
function getUser() { try { return JSON.parse(localStorage.getItem('qvp_user') || 'null'); } catch(e){ return null; } }
function setUser(u) { localStorage.setItem('qvp_user', JSON.stringify(u)); }
function logout() { localStorage.removeItem('qvp_user'); location.href = '/login'; }

function requireLogin() {
  const u = getUser();
  if (!u) { location.href = '/login'; return null; }
  return u;
}

function roleName(r) {
  return { A: '文案事实', B: '图片版权', C: '合规交付' }[r] || r;
}

// 注入侧边栏 + 顶部栏（页面 body 里放 <div id="layout"></div>，内容放 layout 之后）
function renderLayout(active, title) {
  const u = getUser();
  const menu = [
    ['workbench', '/workbench', '📋', '审核工作台'],
    ['import', '/import', '📥', '导入 Query'],
    ['progress', '/progress', '📊', '进度查看'],
    ['dashboard', '/dashboard', '📈', '产能看板'],
  ];
  document.getElementById('layout').innerHTML = `
    <aside class="sidebar">
      <div class="brand">产能验证平台</div>
      <div class="menu-label">功能菜单</div>
      ${menu.map(([key, url, icon, label]) =>
        `<a class="menu-item ${active===key?'active':''}" href="${url}"><span class="icon">${icon}</span>${label}</a>`).join('')}
      <div class="sidebar-footer">
        <div class="user-box">
          ${u ? `<span class="role-badge">${u.role}</span><span class="name">${u.name}</span><button class="logout" onclick="logout()">退出</button>` : ''}
        </div>
      </div>
    </aside>
    <div class="topbar">
      <div class="page-title">${title}</div>
    </div>`;
}
