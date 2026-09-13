"""Smart Moments publishing (image + topic) and on-demand image reading.

All model, network and client calls are mocked: no real WeChat, CDN, paid model
or image generation is touched.
"""
import io
import threading
import time
import unittest
from unittest.mock import patch

import test_moments as fixtures
from test_moments import FID
from PIL import Image
from core import (moments as m, moments_jobs as j, moments_ai, moments_media,
                  moments_native, moments_reflection, account_session as sessions)


def _png(color='red', size=(12, 12)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, format='PNG')
    return buf.getvalue()


def _cdn_xml(text='', media_type='2', host='shmmsns.qpic.cn'):
    return (f'<SnsDataItem><TimelineObject><id>{FID}</id><username>friend-A</username>'
            f'<createTime>1789207200</createTime><contentDesc>{text}</contentDesc>'
            f'<ContentObject><type>1</type><mediaList><media><id>777</id>'
            f'<type>{media_type}</type><url key="k1">https://{host}/full</url>'
            f'<thumb>https://{host}/thumb</thumb><size width="300" height="200"/>'
            f'</media></mediaList></ContentObject></TimelineObject>'
            f'<LocalExtraInfo><nickname>朋友</nickname></LocalExtraInfo></SnsDataItem>')


class Host(unittest.TestCase):
    def test_allowlist_and_ip_and_scheme(self):
        self.assertTrue(moments_media._host_ok('https://shmmsns.qpic.cn/a'))
        self.assertTrue(moments_media._host_ok('http://x.qpic.cn/a'))
        self.assertFalse(moments_media._host_ok('http://127.0.0.1/private'))
        self.assertFalse(moments_media._host_ok('https://1.2.3.4/a'))
        self.assertFalse(moments_media._host_ok('https://evil.com/a'))
        self.assertFalse(moments_media._host_ok('ftp://x.qpic.cn/a'))
        self.assertFalse(moments_media._host_ok('https://qpic.cn.evil.com/a'))

    def test_image_sources_filters_host_and_type(self):
        srcs = moments_media.image_sources(_cdn_xml())
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0]['key'], 'k1')
        self.assertTrue(srcs[0]['url'].endswith('/full'))
        # Video media (type 6) is skipped; private host is dropped.
        self.assertEqual(moments_media.image_sources(_cdn_xml(media_type='6')), [])
        self.assertEqual(moments_media.image_sources(_cdn_xml(host='127.0.0.1')), [])


class Fetch(unittest.TestCase):
    def _resp(self, data):
        class R:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_, n): return data
        return R()

    def test_plaintext_download_ok(self):
        with patch.object(moments_media._opener, 'open', return_value=self._resp(_png())):
            out = moments_media.fetch_image(dict(url='https://shmmsns.qpic.cn/full', key=''))
        self.assertIsNotNone(out)
        self.assertEqual(out[1], 'image/png')

    def test_reject_bad_host_without_network(self):
        with patch.object(moments_media._opener, 'open', side_effect=AssertionError('must not fetch')):
            self.assertIsNone(moments_media.fetch_image(dict(url='https://evil.com/x', key='')))

    def test_oversize_rejected(self):
        big = b'\xff' * (moments_media.MAX_IMAGE_BYTES + 1)
        with patch.object(moments_media._opener, 'open', return_value=self._resp(big)):
            self.assertIsNone(moments_media.fetch_image(dict(url='https://shmmsns.qpic.cn/full', key='')))

    def test_undecodable_bytes_yield_none(self):
        with patch.object(moments_media._opener, 'open', return_value=self._resp(b'not-an-image')):
            self.assertIsNone(moments_media.fetch_image(dict(url='https://shmmsns.qpic.cn/full', key='')))

    def test_token_and_idx_appended_to_url(self):
        seen = {}
        def fake_open(req, timeout=None):
            seen['url'] = req.full_url
            return self._resp(_png())
        with patch.object(moments_media._opener, 'open', side_effect=fake_open):
            moments_media.fetch_image(dict(url='https://shmmsns.qpic.cn/full', key='k1',
                                           token='tok/en+val', idx='1'))
        self.assertIn('token=tok%2Fen%2Bval', seen['url'])
        self.assertIn('idx=1', seen['url'])

    def test_encrypted_payload_degrades_to_none(self):
        class R:
            headers = {'x-Enc': '1'}
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_, n): return _png()  # valid bytes, but flagged encrypted
        with patch.object(moments_media._opener, 'open', return_value=R()):
            # x-Enc:1 means the bytes are an ISAAC-64 stream we can't yet undo, so
            # fetch must not hand the (still-encrypted) payload off as a real image.
            self.assertIsNone(moments_media.fetch_image(dict(url='https://shmmsns.qpic.cn/full', key='9', token='t', idx='1')))


class Describe(unittest.TestCase):
    setUp = fixtures.Moments.setUp

    def setUpCache(self):
        moments_media._desc_cache.clear()

    def test_describe_and_cache(self):
        self.setUpCache()
        calls = []
        def fake_fetch(src):
            calls.append(src)
            return (_png(), 'image/png')
        with patch('core.moments.raw_content', return_value=_cdn_xml()), \
             patch.object(moments_media, 'fetch_image', side_effect=fake_fetch), \
             patch('core.llm.describe_image', return_value='{"status":"success","description":"一只猫"}'), \
             patch('core.llm.load_cfg', return_value={}):
            first = moments_media.describe_feed_images(FID)
            second = moments_media.describe_feed_images(FID)
        self.assertEqual(first, ['一只猫'])
        self.assertEqual(second, ['一只猫'])
        self.assertEqual(len(calls), 1)  # cached second time

    def test_unreadable_is_cached_as_empty(self):
        self.setUpCache()
        with patch('core.moments.raw_content', return_value=_cdn_xml()), \
             patch.object(moments_media, 'fetch_image', return_value=(_png(), 'image/png')), \
             patch('core.llm.describe_image', return_value='{"status":"unreadable","description":""}'), \
             patch('core.llm.load_cfg', return_value={}):
            self.assertEqual(moments_media.describe_feed_images(FID), [])

    def test_describe_tolerates_json_code_fence(self):
        self.setUpCache()
        fenced = '```json\n{"status":"success","description":"一只橘猫"}\n```'
        with patch('core.moments.raw_content', return_value=_cdn_xml()), \
             patch.object(moments_media, 'fetch_image', return_value=(_png(), 'image/png')), \
             patch('core.llm.describe_image', return_value=fenced), \
             patch('core.llm.load_cfg', return_value={}):
            self.assertEqual(moments_media.describe_feed_images(FID), ['一只橘猫'])

    def test_raw_content_failure_is_swallowed(self):
        self.setUpCache()
        with patch('core.moments.raw_content', side_effect=m.Unavailable('no key')):
            self.assertEqual(moments_media.describe_feed_images(FID), [])


class Generate(unittest.TestCase):
    setUp = fixtures.Moments.setUp
    seed = fixtures.Moments.seed

    def test_pure_image_without_vision_skips(self):
        m.ingest([(-1, 'friend-A', _cdn_xml(text=''))])
        item = m.detail(FID)
        with patch('core.moments_media.describe_feed_images', return_value=[]), \
             patch('core.contacts.list_contacts', return_value=[]), \
             patch('core.llm.load_cfg', return_value={}):
            out = moments_ai.generate(dict(feed_id=FID, feed_digest=item['digest']), decide=True)
        self.assertTrue(out.get('skip'))

    def test_vision_enables_comment_on_photo(self):
        m.ingest([(-1, 'friend-A', _cdn_xml(text=''))])
        item = m.detail(FID)
        captured = {}
        def fake_chat(system, msgs, cfg=None):
            captured['user'] = msgs[-1]['content']
            return '{"action":"reply","text":"猫好可爱"}'
        with patch('core.moments_media.describe_feed_images', return_value=['一只橘猫']), \
             patch('core.contacts.list_contacts', return_value=[]), \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.chat', side_effect=fake_chat):
            out = moments_ai.generate(dict(feed_id=FID, feed_digest=item['digest']), decide=True)
        self.assertEqual(out['text'], '猫好可爱')
        self.assertIn('橘猫', captured['user'])
        self.assertIn('media_read', captured['user'])


class Post(unittest.TestCase):
    setUp = fixtures.Moments.setUp

    def _persona(self):
        return dict(error=None, persona=dict(persona='温柔', name='我'))

    def test_generate_post_returns_structured_with_image(self):
        with patch('core.personalization.resolve_persona', return_value=self._persona()), \
             patch('core.moments.catalog', return_value=dict(items=[])), \
             patch('core.moments.settings', return_value=dict(m.DEFAULTS, publish_images=True, publish_web_opinions=True)), \
             patch('core.moments.capabilities', return_value=dict(image_publish=True)), \
             patch('core.tools.web_search', return_value='1. 某新闻\n   摘要'), \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.chat', return_value='{"text":"今天心情不错的一天","image_prompt":"warm scene"}'):
            out = moments_ai.generate_post(['喜悦'])
        self.assertEqual(out['text'], '今天心情不错的一天')
        self.assertEqual(out['image_prompt'], 'warm scene')

    def test_publish_images_off_drops_prompt(self):
        with patch('core.personalization.resolve_persona', return_value=self._persona()), \
             patch('core.moments.catalog', return_value=dict(items=[])), \
             patch('core.moments.settings', return_value=dict(m.DEFAULTS, publish_images=False, publish_web_opinions=False)), \
             patch('core.moments.capabilities', return_value=dict(image_publish=True)), \
             patch('core.tools.web_search', side_effect=AssertionError('should not search')) as ws, \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.chat', return_value='{"text":"安静的午后时光","image_prompt":"scene"}'):
            out = moments_ai.generate_post(['平静'])
        self.assertEqual(out['image_prompt'], '')
        ws.assert_not_called()

    def test_parse_post_tolerates_fence_and_plain(self):
        self.assertEqual(moments_ai._parse_post('```json\n{"text":"a","image_prompt":"b"}\n```'), ('a', 'b'))
        self.assertEqual(moments_ai._parse_post('就一句纯文本'), ('就一句纯文本', ''))

    def test_ai_post_endpoint_returns_plain_text_not_object(self):
        # Regression: generate_post now returns a dict; the /ai-post endpoint must
        # unpack it so the frontend gets a string, not the "[object Object]" that
        # a dict serialised into the text field produces.
        from flask import Flask
        from core import moments_api
        app = Flask(__name__)
        app.register_blueprint(moments_api.create_blueprint(lambda: True))
        client = app.test_client()
        token = sessions.capture()
        with patch('core.moments_ai.generate_post',
                   return_value=dict(text='今天心情不错', image_prompt='sunset')):
            resp = client.post('/api/moments/ai-post', json={'session': token})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data['text'], str)
        self.assertEqual(data['text'], '今天心情不错')
        self.assertEqual(data['image_prompt'], 'sunset')


class ScrollToComment(unittest.TestCase):
    """Off-screen later comments must be scrolled into view before send.

    Pure geometry/loop logic of _scroll_to_comment, driven by scripted OCR and
    avatar frames — no X server. POS puts our post avatar at top=100 in a
    400x800 window; the next post's avatar sits far below.
    """
    POS = dict(x=0, y=0, w=400, h=800, top=100)
    TARGET = dict(id='c9', name='张三', text='目标评论')

    def _native(self, avatars, ocr):
        from unittest.mock import MagicMock
        from core import moments_native
        n = moments_native.Native()
        n._avatars = MagicMock(side_effect=avatars)
        n.ocr = MagicMock(side_effect=ocr)
        n.run = MagicMock()
        return n

    def _hit(self):
        return [dict(text='目标评论', x=50, y=300)]

    def test_target_visible_first_frame_no_scroll(self):
        n = self._native([[100, 700]], [self._hit()])
        with patch('core.docker_wx.priority_pending', return_value=False):
            hit = n._scroll_to_comment(self.POS, self.TARGET)
        self.assertEqual((hit['x'], hit['y']), (50, 300))
        self.assertEqual(n.run.call_count, 0)

    def test_anchored_hit_after_one_scroll(self):
        n = self._native([[100, 700], [95, 700]], [[], self._hit()])
        with patch('core.docker_wx.priority_pending', return_value=False):
            hit = n._scroll_to_comment(self.POS, self.TARGET)
        self.assertEqual(hit['y'], 300)
        self.assertEqual(n.run.call_count, 1)

    def test_tail_mode_after_our_avatar_leaves_top(self):
        # Frame 2 shows only the next post's avatar => our avatar scrolled off.
        n = self._native([[100, 700], [700]], [[], self._hit()])
        with patch('core.docker_wx.priority_pending', return_value=False):
            hit = n._scroll_to_comment(self.POS, self.TARGET)
        self.assertEqual(hit['y'], 300)

    def test_never_found_raises_original_message(self):
        n = self._native([[100, 700]] * 4, [[]] * 4)
        with patch('core.docker_wx.priority_pending', return_value=False):
            with self.assertRaises(moments_native.NativeError) as e:
                n._scroll_to_comment(self.POS, self.TARGET)
        self.assertIn('目标评论未完整显示', str(e.exception))

    def test_two_fuzzy_hits_raise_not_unique(self):
        rows = [dict(text='目标评论一', x=50, y=300), dict(text='目标评论二', x=50, y=360)]
        n = self._native([[100, 700]], [rows])
        with patch('core.docker_wx.priority_pending', return_value=False):
            with self.assertRaises(moments_native.NativeError) as e:
                n._scroll_to_comment(self.POS, self.TARGET)
        self.assertIn('不唯一', str(e.exception))

    def test_priority_pending_yields_to_chat(self):
        n = self._native([[100, 700]], [self._hit()])
        with patch('core.docker_wx.priority_pending', return_value=True):
            with self.assertRaises(moments_native.NativeError) as e:
                n._scroll_to_comment(self.POS, self.TARGET)
        self.assertIn('聊天优先', str(e.exception))

    def test_reply_branch_verifies_then_clicks_in_order(self):
        from unittest.mock import MagicMock
        n = moments_native.Native()
        item = dict(text='帖子正文', name='我', author='me',
                    comments=[dict(id='c9', name='张三', text='目标评论')])
        calls = []
        n.find_post = MagicMock(return_value=self.POS)
        n._scroll_to_comment = MagicMock(return_value=dict(x=50, y=300))
        n.copy_at = MagicMock(side_effect=lambda *a: calls.append('copy_at') or '目标评论')
        n.click = MagicMock(side_effect=lambda *a: calls.append('click'))
        # Abort right after the click, before real editor detection touches X.
        n.shot = MagicMock(side_effect=AssertionError('stop'))
        with self.assertRaises(AssertionError):
            n.prepare_comment(item, 'c9', '回复内容')
        self.assertEqual(calls, ['copy_at', 'click'])

    def _reply_branch(self, copied):
        """Run prepare_comment's reply branch with a stubbed clipboard readback,
        aborting right after the verify/click via a shot() that raises."""
        from unittest.mock import MagicMock
        n = moments_native.Native()
        item = dict(text='帖子正文', name='我', author='me',
                    comments=[dict(id='c9', name='蜜蜜🌱besos', text='他一起回复的。但是概率失败。')])
        clicked = []
        n.find_post = MagicMock(return_value=self.POS)
        n._scroll_to_comment = MagicMock(return_value=dict(x=50, y=300))
        n.copy_at = MagicMock(return_value=copied)
        n.click = MagicMock(side_effect=lambda *a: clicked.append(a))
        n.shot = MagicMock(side_effect=AssertionError('stop-after-click'))
        return n, item, clicked

    def test_reply_comment_copy_with_infix_passes_verification(self):
        # WeChat copies a reply as "昵称 回复 某人：正文"; both name and body present.
        n, item, clicked = self._reply_branch('蜜蜜🌱besos 回复 飒：他一起回复的。但是概率失败。')
        with self.assertRaises(AssertionError):  # aborts at shot(), i.e. past the check + click
            n.prepare_comment(item, 'c9', '回复内容')
        self.assertEqual(len(clicked), 1)

    def test_wrong_comment_copy_fails_verification_without_click(self):
        n, item, clicked = self._reply_branch('完全不相关的另一条评论')
        with self.assertRaises(moments_native.NativeError) as e:
            n.prepare_comment(item, 'c9', '回复内容')
        self.assertIn('核对失败', str(e.exception))
        self.assertEqual(clicked, [])


class PublishImage(unittest.TestCase):
    setUp = fixtures.Moments.setUp

    def test_make_publish_image_off(self):
        with patch('core.moments.settings', return_value=dict(m.DEFAULTS, publish_images=False)), \
             patch('core.llm.gen_image', side_effect=AssertionError('should not draw')):
            self.assertIsNone(j.make_publish_image('x'))

    def test_make_publish_image_ok(self):
        with patch('core.moments.settings', return_value=dict(m.DEFAULTS, publish_images=True)), \
             patch('core.moments.capabilities', return_value=dict(image_publish=True)), \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.gen_image', return_value=_png()):
            aid = j.make_publish_image('warm scene')
        self.assertTrue(aid)
        self.assertTrue(m.asset(aid).exists())

    def test_make_publish_image_failure_degrades(self):
        with patch('core.moments.settings', return_value=dict(m.DEFAULTS, publish_images=True)), \
             patch('core.moments.capabilities', return_value=dict(image_publish=True)), \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.gen_image', side_effect=RuntimeError('no image permission')):
            self.assertIsNone(j.make_publish_image('warm scene'))


class Pipeline(unittest.TestCase):
    setUp = fixtures.Moments.setUp

    def test_process_one_attaches_generated_image(self):
        from core import docker_wx
        p = patch('core.moments.capabilities', return_value=dict(send=True)); p.start(); self.addCleanup(p.stop)
        aid = m.upload(io.BytesIO(_png('blue')))['id']
        captured = {}
        class Fake:
            def prepare_publish(self_, text, paths): captured['text'] = text; captured['paths'] = paths
            def submit(self_): pass
            def cleanup(self_): pass
        with j.db() as c:
            task = j.insert(c, 'auto1', 'publish', 'automatic',
                            dict(settings_revision=0, moods=['喜悦'], assets=[]), time.time())
        with patch.object(docker_wx, 'UI_LOCK', threading.RLock()), \
             patch.object(docker_wx, 'priority_pending', return_value=False), \
             patch('core.moments_native.Native', Fake), \
             patch.object(j, 'policy', return_value=''), \
             patch('core.moments_ai.generate_post', return_value=dict(text='开心的一天', image_prompt='warm')), \
             patch.object(j, 'make_publish_image', return_value=aid), \
             patch('core.moments.catalog', return_value=dict(items=[])), \
             patch('core.moments.sync', return_value=None), \
             patch.object(j, 'receipt', return_value='rid'):
            j.process_one()
        self.assertEqual(captured['paths'], [m.asset(aid)])
        updated = next(x for x in j.listing() if x['id'] == task['id'])
        self.assertEqual(updated['payload']['assets'], [aid])
        self.assertEqual(updated['state'], 'confirmed')


class ChatYield(unittest.TestCase):
    setUp = fixtures.Moments.setUp

    def _job(self):
        with j.db() as c:
            return j.insert(c, 'y1', 'comment', 'automatic',
                            dict(feed_id='F', reply_id='', settings_revision=0, assets=[]), time.time())

    def _state(self, jid):
        with j.db() as c:
            r = c.execute('SELECT state,payload,message FROM moments_jobs WHERE id=?', (jid,)).fetchone()
        return r['state'], j.json.loads(r['payload']), r['message']

    def test_backoff_counts_then_gives_up(self):
        job = self._job()
        for i in range(1, j.MAX_CHAT_YIELD):
            j.yield_to_chat(job, '聊天占用')
            state, payload, _ = self._state(job['id'])
            self.assertEqual(state, 'queued')
            self.assertEqual(payload['attempts'], i)
            self.assertGreater(payload['retry_at'], time.time())
            job['payload'] = payload  # carry attempts forward like process_one does
        j.yield_to_chat(job, '聊天占用')
        state, payload, msg = self._state(job['id'])
        self.assertEqual(state, 'failed')
        self.assertIn('停止重试', msg)

    def test_process_one_skips_until_backoff_elapses(self):
        job = self._job()
        with j.db() as c:
            p = dict(job['payload'], retry_at=time.time() + 999)
            c.execute('UPDATE moments_jobs SET payload=? WHERE id=?', (j.m._json(p), job['id']))
        # retry_at in the future → process_one must leave it queued, untouched.
        j.process_one()
        state, _, msg = self._state(job['id'])
        self.assertEqual(state, 'queued')
        self.assertNotEqual(msg, '正在准备')


class Reflection(unittest.TestCase):
    def test_mood_image_prompt_only_for_expressive(self):
        self.assertTrue(moments_reflection._mood_image_prompt('开心'))
        self.assertTrue(moments_reflection._mood_image_prompt('愤怒'))
        self.assertEqual(moments_reflection._mood_image_prompt('感悟'), '')


if __name__ == '__main__':
    unittest.main()
