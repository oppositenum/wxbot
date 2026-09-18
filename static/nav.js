/* wxbot 统一左侧导航 —— 注入到每个页面，无需改动页面原有结构 */
(function(){
  if (window.__wxnav) return; window.__wxnav = true;
  var LINKS = [
    ['/',                '会话',       '💬'],
    ['/moments',         '朋友圈',     '🖼️'],
    ['/personalization', '画像与人设', '🎭'],
    ['/battle',          '战斗模式',   '⚔️'],
    ['/accounts',        '多账号管理', '👥'],
    ['/desktop',         '扫码登录',   '📱']
  ];
  function norm(p){ p = (p||'').replace(/\/+$/,''); return p || '/'; }
  var here = norm(location.pathname);
  function build(){
    var html = document.documentElement;
    var aside = document.createElement('aside');
    aside.className = 'wxnav';
    var s = '<div class="wxnav-brand">wxbot<small>微信管理后台</small></div><nav class="wxnav-links">';
    LINKS.forEach(function(l){
      var active = norm(l[0]) === here ? ' class="active" aria-current="page"' : '';
      s += '<a href="' + l[0] + '"' + active + '><span class="wxnav-ic">' + l[2] + '</span>' + l[1] + '</a>';
    });
    s += '</nav><div class="wxnav-foot">每个账号，独立管理<br>北京时间 · UTC+8</div>';
    aside.innerHTML = s;

    var scrim = document.createElement('div');
    scrim.className = 'wxnav-scrim';
    scrim.addEventListener('click', function(){ html.classList.remove('wxnav-open'); });

    var toggle = document.createElement('button');
    toggle.className = 'wxnav-toggle';
    toggle.type = 'button';
    toggle.setAttribute('aria-label', '菜单');
    toggle.innerHTML = '☰';
    toggle.addEventListener('click', function(){ html.classList.toggle('wxnav-open'); });

    document.body.appendChild(aside);
    document.body.appendChild(scrim);
    document.body.appendChild(toggle);
    html.classList.add('wxnav-on');
  }
  var lastTouch = 0;
  function bump(){
    var now = Date.now();
    if (now - lastTouch < 15000) return;
    lastTouch = now;
    fetch('/api/auth/touch', {method:'POST', credentials:'same-origin'}).then(function(r){
      if (r.status === 401) location.href = '/login';
    }).catch(function(){});
  }
  ['click','keydown','pointerdown','touchstart'].forEach(function(ev){
    document.addEventListener(ev, bump, true);
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
