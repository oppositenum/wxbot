"""Independent preview integration; only synthetic data and blocked transport."""
if __name__ != '__main__' and __import__('os').environ.get('WXBOT_ACCEPTANCE_CHILD') != '1':
    # The preview installs an intentionally process-global audit hook. Run the
    # unchanged assertions in a real child so the hook cannot contaminate pytest.
    import os as _os, subprocess as _subprocess, sys as _sys
    _env = dict(_os.environ, WXBOT_ACCEPTANCE_CHILD='1')
    _r = _subprocess.run([_sys.executable, __file__], env=_env, text=True,
                         capture_output=True)
    if _r.stdout: print(_r.stdout, end='')
    if _r.stderr: print(_r.stderr, end='', file=_sys.stderr)
    assert _r.returncode == 0, 'acceptance preview child failed'
    import pytest as _pytest
    _pytest.skip('acceptance preview executed in isolated child process', allow_module_level=True)
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.acceptance_preview import build_app, TOKEN, CONTACTS

root=Path(tempfile.mkdtemp(prefix='wxbot-acceptance-check-'))
app,root=build_app(root)
from core import personalization as p, bot, llm, conversation_state as cs
client=app.test_client();headers={'X-Wxbot-Admin-Token':TOKEN}
checks=[]
def check(name, condition):
    assert condition,name
    checks.append(name)
def request(path,body=None):
    return client.get(path,headers=headers) if body is None else client.post(path,json=body,headers=headers)
check('page is original product with synthetic banner',b'MOCK' in client.get('/personalization').data)
check('auth denies missing token',client.get('/api/personalization').status_code==403)
catalog=request('/api/personalization').get_json();session=catalog['session']
check('no startup/page model calls',request('/acceptance/status').get_json()['http_attempts']==0)
check('no background threads',threading.active_count()==1)
check('real sends/webhook/tools/network blocked',all(request('/acceptance/scenario',{'action':'guards'}).get_json()['blocked']))
for name,fn in [('production database',lambda:sqlite3.connect('/tmp/not-acceptance.sqlite3')),('production credential read',lambda:open('llm_config.json')),('external write',lambda:open('/tmp/not-acceptance-write','w')),('subprocess',lambda:os.system('true'))]:
    try:fn()
    except PermissionError:check(name+' blocked',True)
    else:raise AssertionError(name+' not blocked')
for path in ['/api/send','/api/bot/start','/api/schedule/run','/api/push']:
    check(path+' unavailable',client.post(path,json={}).status_code==404)
p.set_global('sun',0,bot.load_rules())
examples=[]
for cid,_ in CONTACTS:
    queries=['说明合成示例','请详细逐步解释这道合成题'] if cid.endswith('A') else ['说明合成示例','请用简短结论回答这次的问题'] if cid.endswith('B') else ['解释这个合成示例']
    for query in queries:
        persona=p.resolve_persona(cid)
        msg=dict(type=1,local_id=700,server_id=70000,sender=cid,is_self=False,content=query,create_time=1788192000)
        with patch('core.memory.select_memories',return_value=[]),patch('core.bot._diag'),patch('core.bot._sender_name',return_value='合成联系人'):
            result=bot._ai_reply(persona['persona'],cid,msg,[],rules={'agent':{'enabled':False}})
        req=json.loads((root/'model-requests.jsonl').read_text().splitlines()[-1])
        examples.append(dict(contact=cid,query=query,role=persona['persona']['name'],source=persona['source'],preferences=p.selected_preferences(cid,query),http_request=req['request'],mock_result=result))
        check(cid+' result explicitly mock '+query,result.startswith('【MOCK'))
        check(cid+' request has ordered behavior/role '+query,req['request']['messages'][0]['content'].index('【通用要求】')<req['request']['messages'][0]['content'].index('【本轮机器人角色】'))
check('A detailed query omits short preference',not any(x['field']=='length' for x in examples[1]['preferences']))
check('B short query omits detailed preference',not any(x['field']=='length' for x in examples[3]['preferences']))
check('C remains no preferences',not examples[4]['preferences'])
start_count=request('/acceptance/status').get_json()['http_attempts']
for action in ['closed','new_inbound','no_proactive','new_inbound']:
    request('/acceptance/scenario',{'action':action})
    state=cs.get('demo_contact_A')
    if action=='closed':check('closure paused',state['paused'])
    if action=='new_inbound':check('new inbound resumes temporary session',not state['paused'])
check('new inbound keeps longterm prohibition',state['no_proactive'])
check('closure zero model calls',request('/acceptance/status').get_json()['http_attempts']==start_count)
(root/'examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2))
(root/'verification.json').write_text(json.dumps(dict(checks=checks,passed=len(checks),model='MOCK ONLY',real_sends=0),ensure_ascii=False,indent=2))
print(json.dumps(dict(passed=len(checks),artifacts=str(root)),ensure_ascii=False))
