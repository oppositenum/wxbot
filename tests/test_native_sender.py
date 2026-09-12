"""Synthetic controller checks only. No WeChat/network/model calls."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from test_send_safety import Isolated, Adapter, forbidden
from core import sender, send_ledger, account_session as sessions
from core.native_sender import NativeContactAdapter


class UI:
    available=True
    def __init__(self):
        self.s=dict(account_folder='account-A',signature='native-login-1',window='1',geometry=dict(X=0,Y=0,WIDTH=1022,HEIGHT=741))
        self.header='header';self.active=True;self.clicks=0;self.stages=0;self.discards=0
    def call(self,action,**kw):
        if action=='state':return copy.deepcopy(self.s)
        if action=='fingerprint':return dict(copy.deepcopy(self.s),header=self.header,active=self.active)
        if action=='stage':self.stages+=1;return {'ok':True}
        if action=='send_once':self.clicks+=1;return {'initiated':True}
        if action=='discard_stage':self.discards+=1;return {'cleared':True}
        raise AssertionError(action)


class NativeSend(Isolated):
    def setUp(self):
        super().setUp();self.ui=UI();self.native=NativeContactAdapter(self.ui)
        self.native.capability=lambda *a:''
        def opened(display,chat):
            self.native.proof=dict(self.token,chat=chat,trusted=True,session_identity='native-login-1')
            self.native.fingerprint=self.ui.call('fingerprint');return True
        self.native.open=opened
        self.rows=[]
        self.native._message_snapshot=lambda chat:self.rows
        mp=patch.object(sender,'_adapter',self.native);mp.start();self.addCleanup(mp.stop)
        self.oldprobe=sessions._identity_probe
        self.addCleanup(lambda:sessions.install_identity_probe(self.oldprobe))

    def send(self, **kw):
        return sender.send_text('same name','test-body',chat_username='chat-A',job_id='job-1',session=kw.get('session',self.token))

    def test_single_click_delayed_receipt_never_replayed_even_restart(self):
        with patch('core.native_sender.time.sleep',lambda *a:None):
            a=self.send();b=self.send();sessions._current=None;sessions.observe();c=self.send(session=sessions.capture())
        self.assertEqual([a['status'],b['status'],c['status']],['uncertain']*3)
        self.assertEqual((self.ui.stages,self.ui.clicks),(1,1))

    def test_confirmed_new_self_text_server_id(self):
        old=self.ui.call
        def call(action,**kw):
            result=old(action,**kw)
            if action=='send_once':self.rows=[dict(local_id=1,server_id=100,sender='account-A',type=1,time=time.time(),digest=send_ledger.stable_id('test-body'))]
            return result
        self.ui.call=call
        self.assertEqual(self.send()['status'],'confirmed');self.assertEqual(self.ui.clicks,1)

    def test_wrong_target_after_stage_never_clicks_and_preserves_outcome(self):
        old=self.ui.call
        def call(action,**kw):
            result=old(action,**kw)
            if action=='stage':self.ui.header='different'
            return result
        self.ui.call=call
        result=self.send();self.assertEqual(result['status'],'not_sent');self.assertEqual(self.ui.clicks,0)
        self.assertFalse(result['retryable'])

    def test_draft_exists_zero_send_no_auto_retry(self):
        old=self.ui.call
        def call(action,**kw):
            if action=='stage':raise RuntimeError('existing_draft')
            return old(action,**kw)
        self.ui.call=call
        result=self.send();self.send();self.assertEqual(result['status'],'not_sent');self.assertEqual(self.ui.clicks,0)

    def test_account_stamp_change_during_generation_invalidates_aba(self):
        sessions.install_identity_probe(lambda:self.ui.s['signature'])
        before=sessions.capture()
        self.ui.s['signature']='native-login-2';sessions.refresh_identity()
        self.ui.s['signature']='native-login-3';sessions.refresh_identity()
        self.assertEqual(sessions.capture()['account'],before['account'])
        self.assertFalse(sessions.valid(before))
        result=self.send(session=before);self.assertEqual(result['status'],'stale');self.assertEqual(self.ui.clicks,0)

    def test_ledger_failure_after_click_leaves_uncertain_not_resend(self):
        update=send_ledger.Ledger.update
        def fail(ledger,jid,status,reason,*a):
            if self.ui.clicks:raise OSError('synthetic disk')
            return update(ledger,jid,status,reason,*a)
        with patch.object(send_ledger.Ledger,'update',fail),patch('core.native_sender.time.sleep',lambda *a:None):a=self.send()
        self.assertEqual(a['status'],'uncertain');self.assertEqual(self.ui.clicks,1)
        self.assertEqual(self.send()['status'],'uncertain');self.assertEqual(self.ui.clicks,1)

    def test_same_text_wrong_user_or_old_id_is_not_receipt(self):
        self.rows=[dict(local_id=3,server_id=1,sender='account-A',type=1,time=time.time(),digest=send_ledger.stable_id('test-body'))]
        old=self.ui.call
        def call(action,**kw):
            result=old(action,**kw)
            if action=='send_once':self.rows.append(dict(local_id=4,server_id=2,sender='other',type=1,time=time.time(),digest=send_ledger.stable_id('test-body')))
            return result
        self.ui.call=call
        with patch('core.native_sender.time.sleep',lambda *a:None):self.assertEqual(self.send()['status'],'uncertain')
        self.assertEqual(self.ui.clicks,1)


class Identity(Isolated):
    def test_same_display_name_cannot_substitute_native_identity(self):
        s=NativeContactAdapter(UI());state=s._state();contact=dict(username='chat-A',identity='unique-A')
        with patch.object(s,'_card',return_value=({},{})),patch.object(s,'_copy_identity',return_value='unique-B'),patch.object(s,'_point',side_effect=forbidden):
            self.assertFalse(s._verify_card(state,contact))
        self.assertIsNone(s.proof)

    def test_changed_second_native_copy_stops_navigation(self):
        s=NativeContactAdapter(UI());state=s._state();contact=dict(username='chat-A',identity='unique-A')
        with patch.object(s,'_card',return_value=({},{})),patch.object(s,'_copy_identity',side_effect=['unique-A','unique-B']),patch.object(s,'_point',side_effect=forbidden):
            self.assertFalse(s._verify_card(state,contact))

    def test_card_navigation_requires_two_matching_copies_and_stable_state(self):
        ui=UI();s=NativeContactAdapter(ui);state=s._state();contact=dict(username='chat-A',identity='unique-A')
        with patch.object(s,'_card',return_value=({},{})),patch.object(s,'_copy_identity',return_value='unique-A') as read,patch.object(s,'_point') as navigate,patch('core.native_sender.time.sleep',lambda *a:None):
            self.assertTrue(s._verify_card(state,contact))
        self.assertEqual(read.call_count,2);self.assertEqual(navigate.call_count,1)
        self.assertEqual(s.identity()['chat'],'chat-A')
        ui.header='changed';self.assertIsNone(s.identity())

    def test_group_and_image_are_explicitly_unsupported(self):
        s=NativeContactAdapter(UI())
        self.assertEqual(s.capability('room@chatroom'),'native_group_identity_unavailable')
        self.assertEqual(s.capability('chat-A','image'),'native_payload_not_supported')

if __name__=='__main__':unittest.main()
