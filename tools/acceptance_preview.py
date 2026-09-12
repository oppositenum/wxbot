#!/usr/bin/env python3
"""Isolated integration preview using the actual product page/API/LLM adapter.

No server.main, poller, scheduler or production account configuration. The HTTP
transport is a deterministic mock. Outbound sockets/processes and writes outside
the marked sandbox are denied with a Python audit hook as well as entry guards.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

TOKEN = 'acceptance-only'
ACCOUNT = 'acceptance_account'
CONTACTS = [('demo_contact_A', '同名测试联系人 · A'), ('demo_contact_B', '同名测试联系人 · B'), ('demo_contact_C', 'C · 资料不足')]
CFG = {'provider':'gpt','gpt':{'base_url':'https://mock.invalid/v1','api_key':'synthetic-not-a-secret','model':'acceptance-mock'},
       'claude':{'base_url':'https://mock.invalid','api_key':'synthetic-not-a-secret','model':'acceptance-mock'}, 'send_temperature':False}


def build_app(data_dir):
    root = Path(data_dir).resolve()
    if root == PROJECT or PROJECT in root.parents:
        raise ValueError('验收数据必须位于项目之外，禁止复用生产目录')
    marker = root / '.wxbot-synthetic-acceptance'
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError('拒绝使用非空且未经标记的数据目录')
    root.mkdir(parents=True, exist_ok=True);marker.write_text('synthetic only\n')
    def audit(event,args):
        if event in ('socket.connect','socket.connect_ex','socket.sendto','subprocess.Popen','os.system','os.posix_spawn'):
            raise PermissionError('ACCEPTANCE: outbound network/process disabled')
        if event=='sqlite3.connect':
            path=Path(os.fsdecode(args[0]).removeprefix('file:').split('?')[0]).resolve()
            if root not in path.parents:raise PermissionError('ACCEPTANCE: non-sandbox database disabled')
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            path=Path(os.fsdecode(args[0])).resolve();mode=args[1];flags=args[2] or 0
            if path in (PROJECT/'llm_config.json',PROJECT/'.env'):raise PermissionError('ACCEPTANCE: production credentials disabled')
            private_roots=[PROJECT/'accounts',PROJECT/'work',PROJECT/'docker/wxdata']
            if any(path==x or x in path.parents for x in private_roots):raise PermissionError('ACCEPTANCE: production data read disabled')
            writing=bool(flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)) or isinstance(mode,str) and any(x in mode for x in 'wax+')
            if writing and root not in path.parents:raise PermissionError('ACCEPTANCE: write outside sandbox disabled')
        if event in ('os.remove','os.rmdir','os.mkdir','os.rename'):
            for value in args[:2] if event=='os.rename' else args[:1]:
                if isinstance(value,(str,bytes,os.PathLike)):
                    path=Path(os.fsdecode(value)).resolve()
                    if path!=root and root not in path.parents:raise PermissionError('ACCEPTANCE: mutation outside sandbox disabled')
    sys.addaudithook(audit)
    os.environ['FLASK_SKIP_DOTENV']='1'
    for key in list(os.environ):
        if key.startswith('WXBOT_LLM_'):os.environ.pop(key)
    import config
    config.ACCOUNTS_DIR=str(root/'accounts');config.WORK_DIR=str(root/'work')
    config.CONTAINER=str(root/'no-wechat');config.DOCKER_DATA=str(root/'no-docker')
    config.wxid=lambda: ACCOUNT
    config.find_accounts=lambda: []
    config.db_storage_dir=lambda: None
    config._ENV_DB_STORAGE=None
    from core import llm, bot, schedule, sender, docker_wx, tools, agent, decrypt, personalization as p, account_session as sessions, profile_drafts as drafts, conversation_state as cs
    sessions._current=None
    llm.CONFIG_FILE=str(root/'synthetic-model-config.json')
    llm.load_cfg=lambda: dict(CFG)
    stats={'mode':'success','http_attempts':0,'blocked_operations':0,'background_threads_started':0}
    def forbidden(*args,**kwargs):
        stats['blocked_operations']+=1
        raise RuntimeError('ACCEPTANCE: external action disabled')
    # Prevent calls even if a future management endpoint accidentally reaches them.
    for mod,names in [(sender,['send_text','send_relay','send_image','send_at','send_webhook','retry','_send','focus_chat']),
                      (docker_wx,['send_text','send_image','send_at','open_chat']),
                      (tools,['run']), (agent,['run']), (decrypt,['run']),
                      (llm,['gen_image','transcribe','describe_image']),
                      (bot,['main','run_once','greet','run_follow','_maybe_nudge']),
                      (schedule,['start_loop','_loop'])]:
        for name in names:
            if hasattr(mod,name):setattr(mod,name,forbidden)
    def mock_post(url,headers,body,proxy,timeout=60,retries=3):
        stats['http_attempts']+=1
        # Only mock transport receives this body; never creates an HTTP socket.
        with (root/'model-requests.jsonl').open('a') as f:
            f.write(json.dumps({'mock':True,'request':body,'retries':retries},ensure_ascii=False)+'\n')
        if stats['mode']=='failure':raise TimeoutError('synthetic timeout')
        history=body.get('messages',[]);user=next((m['content'] for m in reversed(history) if m['role']=='user'),'')
        try:req=json.loads(user)
        except (TypeError,ValueError):req={}
        if 'allowed_dimensions' in req:
            rows=req['messages'];scope=req['scope']
            if len(rows)<3:items=[]
            else:
                value='详细' if any('详细' in m['text'] for m in rows) else '简短'
                items=[dict(category='reception',dimension='length',value=value,evidence_ids=[m['id'] for m in rows],
                            rationale='【MOCK 合成输出】根据样本中的明确长期反馈生成验收草稿',conflict_ids=[],confidence='medium',sufficient=True,scope=scope)]
            response=json.dumps({'items':items},ensure_ascii=False)
        else:response='【MOCK 合成输出，不是真实模型生成】按当前请求及所选角色、偏好组织回答。'
        if 'system' in body:return {'content':[{'type':'text','text':response}]}
        return {'choices':[{'message':{'content':response}}]}
    llm._post=mock_post
    # Seed once. Ordinary restarts preserve synthetic review progress.
    acct=Path(config.account_dir());dbdir=acct/'decrypted';dbdir.mkdir(parents=True,exist_ok=True)
    if not (root/'seeded.json').exists():
        for slug,name,persona in [('sun','晨光 · 全局角色','你是晨光，一位诚实、干练的助手。'),('moon','明月 · 专属角色','你是明月，一位耐心、温和的助手。')]:
            path=Path(config.personas_dir())/(slug+'.json');path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(dict(slug=slug,name=name,persona=persona,samples=[]),ensure_ascii=False))
        (acct/'bot_rules.json').write_text(json.dumps({'watch':[],'proactive':{'enabled':False},'rules':[{'name':'synthetic-legacy-rule','match':{'type':'auto'},'action':{'type':'reply_ai','persona':'sun'}}]},ensure_ascii=False))
        con=sqlite3.connect(dbdir/'contact.db');con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,alias TEXT,local_type INTEGER)')
        con.executemany('INSERT INTO contact VALUES(?,?,?,?,?)',[(cid,'同名测试联系人' if i<2 else name,name,cid,1) for i,(cid,name) in enumerate(CONTACTS)]);con.execute('CREATE TABLE chat_room(username TEXT,owner TEXT,ext_buffer BLOB)');con.commit();con.close()
        from core import messages
        con=sqlite3.connect(dbdir/'message.db');con.execute('CREATE TABLE Name2Id(user_name TEXT)');con.executemany('INSERT INTO Name2Id VALUES(?)',[(ACCOUNT,)]+[(cid,) for cid,_ in CONTACTS])
        for n,(cid,_) in enumerate(CONTACTS):
            table=messages.msg_table(cid);con.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY, server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,source TEXT,packed_info_data BLOB)')
            for i in range(12 if n<2 else 1):
                ts=1788192000+(i//3)*86400+(i%3)*60
                content=('以后回复详细一点' if n==1 else '以后回复短一点') if i%3==0 else '合成日常问题，请说明这个示例。'
                if n==2:content='你好'
                con.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',(i+1,1000+n*100+i,1,n+2,ts,content,'',None))
        con.commit();con.close()
        p.update('demo_contact_A',{'auto_update':True,'personalization_enabled':True,'conversation_control_enabled':True},0)
        p.learn_live('demo_contact_A',[dict(local_id=1,server_id=1000,type=1,is_self=False,sender='demo_contact_A',content='以后回复短一点',create_time=1788192000)])
        p.update('demo_contact_A',{'preferences':{'tone':{'value':'直接','locked':False}}},p.get('demo_contact_A')['revision'])
        p.update('demo_contact_B',{'persona_id':'moon','personalization_enabled':True,'preferences':{'length':{'value':'详细'},'tone':{'value':'温和'}}},0)
        (root/'seeded.json').write_text(json.dumps({'synthetic':True,'version':1}))
    # Use original backend auth and Blueprint, without its server or background boot.
    os.environ['WXBOT_ADMIN_READ_TOKEN']=TOKEN
    import server
    from flask import Flask, jsonify, request, Response
    app=Flask('wxbot_acceptance',static_folder=None)
    app.register_blueprint(server.personalization_bp)
    app.before_request(server.authorize_private_management)
    @app.before_request
    def acceptance_only():
        if request.path.startswith('/acceptance/') and request.headers.get('X-Wxbot-Admin-Token')!=TOKEN:
            return jsonify(error='验收入口需要合成管理员 Token'),403
    @app.get('/')
    @app.get('/personalization')
    def page():
        html=(PROJECT/'static/personalization.html').read_text()
        banner='<aside style="background:#fff1b8;padding:14px;border:2px solid #b58800">独立集成验收 · 全部为合成数据 · 模型为 MOCK · 微信/Webhook/外部写操作已阻断<br>验收 Token：acceptance-only；样本日期：2026-09-01 至 2026-09-04。无后台发送任务。</aside>'
        return Response(html.replace('<main>','<main>'+banner),mimetype='text/html')
    @app.get('/favicon.ico')
    def favicon():return '',204
    @app.get('/acceptance/status')
    def status():return jsonify(dict(stats,synthetic=True,account=ACCOUNT,session=sessions.capture(),pid=os.getpid(),data_dir=str(root)))
    @app.post('/acceptance/scenario')
    def scenario():
        body=request.get_json() or {};cid=body.get('contact','demo_contact_A')
        if cid not in dict(CONTACTS):return jsonify(error='仅限合成联系人'),400
        action=body.get('action')
        if action=='model_mode':
            if body.get('mode') not in ('success','failure'):return jsonify(error='invalid mode'),400
            stats['mode']=body['mode']
        elif action=='concurrent_edit':p.update(cid,{'auto_update':not p.get(cid)['auto_update']},p.get(cid)['revision'])
        elif action in ('closed','new_inbound','no_proactive'):
            text={'closed':'先这样，拜拜','new_inbound':'新入站合成问题','no_proactive':'以后不要主动给我发消息'}[action]
            last=cs.get(cid)['last_id'];cs.observe(cid,[dict(type=1,is_self=False,sender=cid,local_id=last+100,server_id=last+90000,content=text)])
        elif action=='interrupted':
            result=drafts.preview(cid,1788192000,1788537600,4,3,60000);jid=result['job']['id']
            with p._db(True) as con:
                row=con.execute('SELECT data FROM analysis_batches WHERE job=? AND ordinal=0',(jid,)).fetchone();data=json.loads(row['data']);data.update(generation='synthetic-prior-process',attempt='synthetic-interrupted',attempt_history=[{'id':'synthetic-interrupted','status':'reserved'}])
                con.execute("UPDATE analysis_batches SET status='running',attempts=1,data=? WHERE job=? AND ordinal=0",(json.dumps(data),jid))
                row=con.execute('SELECT data FROM analysis_jobs WHERE id=?',(jid,)).fetchone();job=json.loads(row['data']);job.update(calls_reserved=1,tokens_reserved=data['input_estimate']+drafts.MAX_OUTPUT,status='analyzing');con.execute('UPDATE analysis_jobs SET data=? WHERE id=?',(json.dumps(job),jid))
            return jsonify(id=jid)
        elif action=='guards':
            probes=[lambda:sender.send_text('synthetic','text'),lambda:sender.send_webhook('https://mock.invalid',{}),lambda:tools.run('generate_image',{},None),lambda:socket.create_connection(('127.0.0.1',1))]
            outcomes=[]
            for probe in probes:
                try:probe();outcomes.append(False)
                except (RuntimeError,PermissionError):outcomes.append(True)
            return jsonify(blocked=outcomes)
        else:return jsonify(error='未知验收场景'),400
        return jsonify(ok=True)
    return app,root


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True);parser.add_argument('--port',type=int,default=5188);args=parser.parse_args()
    if args.port==5100 or not 1024<=args.port<=65535:parser.error('必须使用非生产独立端口')
    app,root=build_app(args.data_dir)
    (root/'preview.pid').write_text(str(os.getpid()))
    print(f'ACCEPTANCE ONLY http://127.0.0.1:{args.port}/personalization token={TOKEN}',flush=True)
    app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False,threaded=True,load_dotenv=False)

if __name__=='__main__':main()
