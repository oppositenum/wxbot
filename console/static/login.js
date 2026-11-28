document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const go = document.getElementById('go'), err = document.getElementById('err');
  go.disabled = true; err.textContent = '';
  try {
    const r = await fetch('/api/console/login', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({username: document.getElementById('u').value.trim(), password: document.getElementById('p').value})});
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) { location.replace('/'); return; }
    err.textContent = d.error || '登录失败';
  } catch (x) { err.textContent = '连不上控制台'; }
  go.disabled = false;
};
document.getElementById('u').focus();
