"""Registry for isolated multi-WeChat account environments."""
import json, os, re, shutil, subprocess, time
from pathlib import Path
import config

REGISTRY = Path(config.WORK_DIR) / 'multi_accounts.json'
IMAGE = os.environ.get('WXBOT_MULTI_IMAGE', 'wxbot-ubuntu-manual:24.04')
BASE_WEB = int(os.environ.get('WXBOT_MULTI_WEB_BASE', '5100'))
BASE_VNC = int(os.environ.get('WXBOT_MULTI_VNC_BASE', '6082'))

def _read():
    try:
        value = json.loads(REGISTRY.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {'accounts': []}
    except (OSError, ValueError): return {'accounts': []}

def _write(value):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix('.tmp'); tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8'); os.replace(tmp, REGISTRY)

def _status(container):
    if not container: return {'container': 'unconfigured', 'running': False, 'online': False}
    if not shutil.which('docker'): return {'container': 'docker_unavailable', 'running': False, 'online': False}
    try:
        state = subprocess.check_output(['docker','inspect','--format','{{.State.Status}}',container], stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
    except Exception: state = 'stopped'
    return {'container': state, 'running': state == 'running', 'online': False}

def list_accounts():
    accounts = [a for a in _read().get('accounts', []) if isinstance(a, dict)]
    if not accounts and (os.environ.get('WXBOT_MULTI_INCLUDE_LEGACY', '1') == '1'):
        # Present the existing single-account deployment as the primary instance
        # until the operator explicitly registers additional accounts.
        accounts = [{'id':'primary','label':'当前微信号','container':'wxbot-ubuntu-manual',
                     'image':IMAGE,'web_port':5100,'vnc_port':6082,
                     'volume':'wxbot-ubuntu-manual-home','enabled':True,'legacy':True}]
    result=[]
    for a in accounts:
        status = {'container':'managed_externally','running':True,'online':False} if a.get('legacy') else _status(a.get('container'))
        result.append({**a, 'status': status})
    return result

def create(label, account_id=None):
    data = _read(); accounts = data.setdefault('accounts', [])
    slug = account_id or re.sub(r'[^a-z0-9-]+', '-', label.lower()).strip('-')
    if not slug or any(a.get('id') == slug for a in accounts): raise ValueError('账号 ID 已存在或无效')
    n = len(accounts)
    account = {'id': slug, 'label': label.strip()[:100], 'container': 'wxbot-'+slug, 'image': IMAGE,
               'web_port': BASE_WEB+n+1, 'vnc_port': BASE_VNC+n+1, 'volume': 'wxbot-'+slug+'-home',
               'appdata': str(Path(config.ACCOUNTS_DIR)/slug), 'enabled': True, 'created_at': int(time.time())}
    accounts.append(account); _write(data); return account

def action(account_id, operation):
    account = next((a for a in _read().get('accounts', []) if a.get('id') == account_id), None)
    if not account: raise KeyError('账号不存在')
    if operation not in ('start','stop','restart'): raise ValueError('不支持的操作')
    if not shutil.which('docker'): return {'ok': False, 'reason': 'docker_unavailable', 'account': account}
    try: subprocess.run(['docker', operation, account['container']], check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.CalledProcessError) as exc: return {'ok': False, 'reason': str(exc)[-400:], 'account': account}
    return {'ok': True, 'account': account, 'status': _status(account['container'])}
