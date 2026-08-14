// 共享：登录状态 + 导航栏
function getUser() { try { return JSON.parse(localStorage.getItem('qvp_user') || 'null'); } catch(e){ return null; } }
function setUser(u) { localStorage.setItem('qvp_user', JSON.stringify(u)); }
function logout() { localStorage.removeItem('qvp_user'); location.href = '/login'; }

function requireLogin() {
  const u = getUser();
  if (!u) { location.href = '/login'; return null; }
  return u;
}

// 注入顶部导航栏（页面 body 里放 <div id="nav"></div>）
function renderNav(active) {
  const u = getUser();
  const links = [
    ['workbench', '/workbench', '审核工作台'],
    ['import', '/import', '导入 Query'],
    ['progress', '/progress', '进度查看'],
    ['dashboard', '/dashboard', '产能看板'],
  ];
  const html = `
    <div class="brand">产能验证平台</div>
    ${links.map(([key, url, label]) => `<a href="${url}" class="${active===key?'active':''}">${label}</a>`).join('')}
    <div class="spacer"></div>
    <div class="userbox">
      ${u ? `<span class="role-badge">${roleName(u.role)}</span><span>${u.name}</span><a href="#" class="logout" onclick="logout();return false;">切换/退出</a>` : ''}
    </div>`;
  document.getElementById('nav').innerHTML = html;
}

function roleName(r) {
  return { A: 'A 文案事实', B: 'B 图片版权', C: 'C 合规交付' }[r] || r;
}
