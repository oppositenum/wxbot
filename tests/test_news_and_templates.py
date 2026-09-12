"""Offline news XML and account-isolated default template regression tests."""
import unittest
from unittest.mock import patch
from test_contact_personalization import Isolated
from core import messages, personalization as p, account_session as sessions


class News(unittest.TestCase):
    def test_news_reader_and_single_article(self):
        xml = '<mmreader><category><item><title>新闻一</title><url>https://example.com/1</url></item><item><title>新闻二</title><url>https://example.com/2</url></item></category></mmreader>'
        value = messages._parse_appmsg(xml)
        self.assertEqual(len(value['articles']), 2)
        self.assertEqual(len(messages._parse_appmsg(xml.replace('<item>', '<newitem>').replace('</item>', '</newitem>'))['articles']), 2)
        self.assertEqual(value['title'], '新闻一')
        duplicate = '<mmreader><category><item><title>同篇</title><url>https://example.com/1?a=1</url></item><newitem><title>同篇</title><url>https://example.com/1?a=2</url></newitem></category></mmreader>'
        self.assertEqual(len(messages._parse_appmsg(duplicate)['articles']), 1)
        single = messages._parse_appmsg('<mmreader><category><item><title>单篇</title></item></category></mmreader>')
        self.assertEqual(single['title'], '单篇')

    def test_appmsg_and_malformed(self):
        self.assertEqual(messages._parse_appmsg('<appmsg><title>链接</title><url>https://example.com</url></appmsg>')['title'], '链接')
        self.assertIsNone(messages._parse_appmsg('<mmreader>'))
        self.assertIsNone(messages._parse_appmsg('普通文字'))


class Templates(Isolated):
    def setUp(self):
        super().setUp()
        m=patch.object(p, '_template_target', lambda c: c.startswith('friend-'))
        m.start();self.addCleanup(m.stop)
        self.change('friend-A', persona_id='Q', personalization_enabled=True, auto_update=True,
                    preferences={'tone':{'value':'温和','locked':True}})

    def test_inheritance_override_and_account_boundary(self):
        p.save_template('friend-A', 0)
        inherited=p.get('friend-B')
        self.assertEqual(self.role('friend-B')['persona_id'], 'Q')
        self.assertEqual(inherited['preferences']['tone']['scope'], 'chat:friend-B')
        self.assertEqual(inherited['preferences']['tone']['evidence_ids'], [])
        self.assertNotIn('inherited_template', p.get('group@chatroom'))
        self.assertNotIn('inherited_template', p.get('newsapp'))
        self.change('friend-B', preferences={'tone':{'value':'直接'}})
        self.change('friend-A', preferences={'tone':{'value':'轻松'}})
        p.save_template('friend-A', 1)
        self.assertEqual(p.get('friend-B')['preferences']['tone']['value'], '直接')
        self.account='account-B';sessions.observe()
        self.assertFalse(p.get('friend-B')['personalization_enabled'])

    def test_apply_revisions_and_disable(self):
        p.save_template('friend-A', 0)
        self.change('friend-B', persona_id='P')
        with self.assertRaises(p.Conflict):p.apply_template('friend-B', 0, 1)
        out=p.apply_template('friend-B', 1, 1)
        self.assertEqual(out['persona_id'], 'Q')
        self.assertEqual(out['revision'], 2)
        p.save_template('friend-A', 1, enabled=False)
        self.assertFalse(p.get('friend-C')['personalization_enabled'])
        self.assertEqual(p.get('friend-B')['persona_id'], 'Q')

if __name__ == '__main__':unittest.main()
