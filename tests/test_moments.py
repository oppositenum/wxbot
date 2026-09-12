"""Isolated Moments parsing/storage/API checks; no WeChat, network or model sends."""
import io
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from PIL import Image
import config
from core import account_session as sessions, moments as m
from core.moments_api import create_blueprint

FID = '18446744073709551615'
def xml(text='你好 &amp; 世界', comments=True):
    return f'''<SnsDataItem><TimelineObject><id>{FID}</id><username>friend-A</username><createTime>1789207200</createTime><contentDesc>{text}</contentDesc><ContentObject><type>1</type><mediaList><media><id>123</id><type>2</type><url key="secret" token="secret">http://127.0.0.1/private</url><size width="300" height="200"/></media></mediaList></ContentObject></TimelineObject><LocalExtraInfo><nickname>朋友 &lt;script&gt;</nickname></LocalExtraInfo>{'<comment_user_list><user_comment><username>friend-B</username><nickname>朋友乙</nickname><comment_64id>99999999999999999</comment_64id><content>好看</content><create_time>1789207300</create_time><ref_username>friend-A</ref_username></user_comment><user_comment><b_deleted>1</b_deleted><content>已删除</content></user_comment></comment_user_list>' if comments else ''}<like_user_list><user_comment><username>friend-C</username></user_comment></like_user_list></SnsDataItem>'''

class Moments(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = 'account-A'
        for target, value in [('config.ACCOUNTS_DIR', self.temp.name), ('config.wxid', lambda: self.account),
                              ('core.account_session._identity_probe', None), ('core.account_session._current', None)]:
            p = patch(target, value);p.start();self.addCleanup(p.stop)
        self.session = sessions.capture()
        self.authorized = True
        app = Flask(__name__)
        app.register_blueprint(create_blueprint(lambda: self.authorized))
        self.client = app.test_client()

    def post(self, path, body=None):
        return self.client.post('/api/moments'+path, json=dict(body or {}, session=self.session))

    def seed(self):
        m.ingest([(-1, 'friend-A', xml())])
        return m.detail(FID)

    def test_parse_identity_big_ids_and_safe_media(self):
        v = m.parse_feed(-1, 'friend-A', xml())
        self.assertEqual(v['id'], FID)
        self.assertEqual(v['text'], '你好 & 世界')
        self.assertEqual(len(v['comments']), 1)
        self.assertEqual(v['comments'][0]['id'], '99999999999999999')
        legacy = xml().replace('<comment_64id>99999999999999999</comment_64id>',
                               '<comment_64id>0</comment_64id><comment_id>42</comment_id>')
        self.assertEqual(m.parse_feed(-1, 'friend-A', legacy)['comments'][0]['id'], '42')
        self.assertEqual(len(v['likes']), 1)
        self.assertNotIn('secret', json.dumps(v))
        self.assertNotIn('127.0.0.1', json.dumps(v))
        for data in [xml().replace(FID,'1'), '<!DOCTYPE x>'+xml(), xml().replace('friend-A','intruder'), '<broken>']:
            with self.assertRaises((ValueError, m.ET.ParseError)):
                m.parse_feed(-1, 'friend-A', data)

    def test_incremental_snapshot_and_parse_failure_preserve_cache(self):
        a=m.ingest([(-1,'friend-A',xml())]);self.assertEqual(a['added'],1)
        b=m.ingest([(-1,'friend-A',xml())]);self.assertEqual((b['added'],b['changed']),(0,0))
        c=m.ingest([(-1,'friend-A',xml('更新',False))]);self.assertEqual(c['changed'],1)
        self.assertEqual(m.detail(FID)['comments'],[])
        with self.assertRaises(m.Unavailable):m.ingest([(-1,'friend-A','broken')])
        self.assertEqual(m.detail(FID)['text'],'更新')
        with m.database() as c:
            m._set(c, 'sync_error', {'message':'old failure'})
        m.ingest([])  # disappearance from current cache is not a deletion proof
        self.assertIsNone(m.status()['sync_error'])
        self.assertEqual(m.catalog()['total'],1)
        self.assertEqual(m.catalog(search='更新')['total'],1)
        self.assertEqual(m.catalog(search='%')['total'],0)
        self.assertEqual(m.catalog(author="' OR 1=1 --")['total'],0)

    def test_account_isolation_and_epoch_mutations(self):
        self.seed()
        a=self.post('/drafts',dict(kind='publish',text='账号甲草稿')).get_json()['draft']
        self.account='account-B';sessions.observe()
        self.assertEqual(m.catalog()['total'],0)
        self.assertEqual(m.drafts(),[])
        self.assertEqual(self.post('/drafts',dict(kind='publish',text='旧页')).status_code,409)
        self.assertEqual(self.client.get('/api/moments?session='+json.dumps(self.session)).status_code,409)
        self.account='account-A';sessions.observe()
        self.assertEqual(len(m.drafts()),1)
        self.assertEqual(m.drafts()[0]['id'],a['id'])
        self.assertEqual(self.post('/drafts',dict(kind='publish',text='旧代次')).status_code,409)

    def test_draft_revision_target_and_comment_reference(self):
        item=self.seed()
        body=dict(kind='comment',feed_id=FID,feed_digest=item['digest'],reply_id='99999999999999999',text='谢谢分享')
        d=m.save_draft(body)
        self.assertEqual(d['status'],'draft')
        with self.assertRaises(m.Conflict):m.save_draft(dict(body,id=d['id'],revision=0))
        m.ingest([(-1,'friend-A',xml('目标改变'))])
        with self.assertRaises(m.Conflict):m.save_draft(body)
        with self.assertRaises(m.Conflict):m.discard_draft(d['id'],0)
        m.discard_draft(d['id'],1)
        self.assertEqual(m.drafts()[0]['status'],'discarded')
        with self.assertRaises(m.Conflict):m.save_draft(dict(body,id=d['id'],revision=2))

    def test_auth_private_media_and_disabled_send(self):
        self.authorized=False
        for path in ['', '/drafts', '/assets/'+'a'*64]:
            self.assertEqual(self.client.get('/api/moments'+path).status_code,403)
        self.assertEqual(self.post('/sync').status_code,403)
        self.authorized=True
        r=self.client.get('/api/moments');self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertFalse(r.get_json()['capabilities']['send'])
        self.assertEqual(self.post('/drafts/'+'b'*32+'/submit').status_code,503)
        self.assertEqual(self.post('/settings',dict(revision=0,patch={'auto_comment':True})).status_code,503)
        self.assertFalse(m.settings()['auto_comment'])

    def test_settings_cas_validation_and_china_quiet_boundaries(self):
        s=m.settings();self.assertFalse(s['sync_enabled']);self.assertFalse(s['auto_publish'])
        s=m.save_settings(dict(quiet_start='22:00',quiet_end='08:00'),0)
        self.assertEqual(s['revision'],1)
        with self.assertRaises(m.Conflict):m.save_settings({},0)
        for p in [{'sync_enabled':'false'},{'quiet_start':'25:00'}, {'quiet_start':'08:00'},
                  {'sync_interval_minutes':0},{'daily_publish_limit':True},{'friend_allowlist':['unknown']}]:
            with patch('core.contacts.list_contacts',return_value=[]):
                with self.assertRaises(ValueError):m.save_settings(p,1)
        for clock,expected in [('21:59',False),('22:00',True),('00:00',True),('07:59',True),('08:00',False)]:
            ts=datetime.fromisoformat('2026-09-12T'+clock+':00+08:00').timestamp()
            self.assertEqual(m.quiet_now(s,ts),expected)
        s.update(quiet_start='12:00',quiet_end='14:00')
        self.assertTrue(m.quiet_now(s,datetime.fromisoformat('2026-09-12T05:00:00+00:00').timestamp()))

    def test_upload_validation_isolation_and_preview(self):
        buf=io.BytesIO();Image.new('RGB',(10,20),'red').save(buf,format='PNG');raw=buf.getvalue()
        data={'session':json.dumps(self.session),'file':(io.BytesIO(raw),'../../evil.jpg')}
        r=self.client.post('/api/moments/upload',data=data,content_type='multipart/form-data')
        self.assertEqual(r.status_code,200)
        aid=r.get_json()['asset']['id']
        self.assertEqual(len(aid),64)
        self.assertFalse(Path(self.temp.name,'evil.jpg').exists())
        self.assertEqual(self.client.get('/api/moments/assets/'+aid).status_code,409)
        r=self.client.get('/api/moments/assets/'+aid,query_string={'session':json.dumps(self.session)})
        self.assertEqual(r.status_code,200);self.assertEqual(r.mimetype,'image/jpeg')
        m.save_draft(dict(kind='publish',text='',assets=[aid]))
        with self.assertRaises(ValueError):m.upload(io.BytesIO(b'<svg>not an image</svg>'))
        self.account='account-B';sessions.observe()
        with self.assertRaises(ValueError):m.asset(aid)
        with self.assertRaises(ValueError):m.save_draft(dict(kind='publish',text='x',assets=[aid]))

    def test_periodic_default_and_failed_attempt_backoff(self):
        with patch.object(m,'sync',side_effect=RuntimeError('fail')) as sync:
            self.assertEqual(m.periodic_once(now=10000),'disabled');sync.assert_not_called()
            m.save_settings({'sync_enabled':True},0)
            self.assertEqual(m.periodic_once(now=10000),'failed');self.assertEqual(sync.call_count,1)
            self.assertEqual(m.periodic_once(now=10001),'waiting');self.assertEqual(sync.call_count,1)
            self.assertIsNotNone(m.status()['sync_error'])
            sync.side_effect=None
            self.assertEqual(m.periodic_once(now=10600),'synced');self.assertIsNone(m.status()['sync_error'])

    def test_ai_drafts_selected_context_single_attempt_no_send(self):
        item=self.seed()
        from core import moments_ai
        cfg={'provider':'gpt','gpt_model':'configured-model','single_attempt':False}
        with patch.object(moments_ai.contacts,'list_contacts',return_value=[]), \
             patch.object(moments_ai.llm,'load_cfg',return_value=cfg), \
             patch.object(moments_ai.llm,'chat',return_value=' 真好看！ ') as chat:
            r=self.post('/ai-reply',dict(feed_id=FID,feed_digest=item['digest'],reply_id='99999999999999999'))
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.get_json()['text'],'真好看！')
            self.assertFalse(r.get_json()['sent'])
            args=chat.call_args
            self.assertEqual(args.kwargs['cfg']['gpt_model'],'configured-model')
            self.assertTrue(args.kwargs['cfg']['single_attempt'])
            self.assertFalse(cfg['single_attempt'])
            context=json.loads(args.args[1][0]['content'])
            self.assertEqual(context['reply_to']['text'],'好看')
            self.assertFalse(context['post']['media_read'])
            self.assertFalse(context['speaker']['is_post_author'])
            self.assertNotIn('friend-C',json.dumps(context))
            self.assertEqual(m.drafts(),[])  # generation alone never saves or submits

    def test_ai_own_post_answers_friend_as_publisher(self):
        from core import moments_ai
        own_xml=xml('[脸红]').replace('friend-A','account-A').replace('好看','这是什么表情？')
        m.ingest([(-1,'account-A',own_xml)])
        item=m.detail(FID)
        with patch.object(moments_ai.contacts,'list_contacts',return_value=[]), \
             patch.object(moments_ai.llm,'load_cfg',return_value={'provider':'gpt'}), \
             patch.object(moments_ai.llm,'chat',return_value='我发的是脸红表情，有点害羞的意思。') as chat:
            r=self.post('/ai-reply',dict(feed_id=FID,feed_digest=item['digest'],reply_id=''))
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.get_json()['reply_id'],'99999999999999999')
            system,msgs=chat.call_args.args
            data=json.loads(msgs[0]['content'])
            self.assertTrue(data['speaker']['is_post_author'])
            self.assertTrue(data['post']['is_self'])
            self.assertFalse(data['reply_to']['is_self'])
            self.assertEqual(data['reply_to']['text'],'这是什么表情？')
            self.assertIn('以第一人称回应朋友的评论',system)
            self.assertIn('不要把自己的动态当作别人发的',system)

    def test_ai_rejects_stale_target_auth_and_picture_only_before_model(self):
        item=self.seed()
        from core import moments_ai
        with patch.object(moments_ai.llm,'chat') as chat:
            self.authorized=False
            self.assertEqual(self.post('/ai-reply',{}).status_code,403)
            self.authorized=True
            self.assertEqual(self.post('/ai-reply',dict(feed_id=FID,feed_digest='old')).status_code,409)
            self.assertEqual(self.post('/ai-reply',dict(feed_id=FID,feed_digest=item['digest'],reply_id='missing')).status_code,409)
            m.ingest([(-1,'friend-A',xml('',False))]);item=m.detail(FID)
            self.assertEqual(self.post('/ai-reply',dict(feed_id=FID,feed_digest=item['digest'])).status_code,503)
            chat.assert_not_called()

    def test_ai_failure_empty_output_and_switch_during_generation(self):
        item=self.seed()
        from core import moments_ai
        body=dict(feed_id=FID,feed_digest=item['digest'])
        with patch.object(moments_ai.contacts,'list_contacts',return_value=[]), \
             patch.object(moments_ai.llm,'load_cfg',return_value={'provider':'gpt'}), \
             patch.object(moments_ai.llm,'chat') as chat:
            chat.side_effect=RuntimeError('secret-token-upstream-error')
            r=self.post('/ai-reply',body);self.assertEqual(r.status_code,503)
            self.assertNotIn('secret-token',r.get_data(as_text=True))
            chat.side_effect=None;chat.return_value=' '
            self.assertEqual(self.post('/ai-reply',body).status_code,503)
            def switch(*a,**kw):
                self.account='account-B';sessions.observe();return '旧账号生成结果'
            chat.side_effect=switch
            r=self.post('/ai-reply',body);self.assertEqual(r.status_code,409)
            self.assertNotIn('旧账号生成结果',r.get_data(as_text=True))

    def test_ai_uses_recipient_role_and_rechecks_digest(self):
        item=self.seed()
        from core import moments_ai
        with patch.object(moments_ai.contacts,'list_contacts',return_value=[{'username':'friend-A'}]), \
             patch.object(moments_ai.personalization,'resolve_persona',return_value={'error':'','persona':{'name':'角色甲','persona':'角色正文'}}) as resolve, \
             patch.object(moments_ai.personalization,'role_context',return_value='已选人设与偏好') as role, \
             patch.object(moments_ai.llm,'load_cfg',return_value={'provider':'gpt'}), \
             patch.object(moments_ai.llm,'chat') as chat:
            def changed(*a,**kw):
                self.assertIn('已选人设与偏好',a[0]);m.ingest([(-1,'friend-A',xml('变了'))]);return '旧上下文'
            chat.side_effect=changed
            r=self.post('/ai-reply',dict(feed_id=FID,feed_digest=item['digest']))
            self.assertEqual(r.status_code,409)
            resolve.assert_called_once_with('friend-A')
            role.assert_called_once()

if __name__=='__main__':unittest.main()
