"""Synthetic local-media and retired-UI tests; no live model, WeChat or network."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
from PIL import Image
from test_send_safety import Isolated, forbidden
from core import imgdec, local_media, harvest, media


def png(size=(40,30)):
    buf=io.BytesIO();Image.new('RGB',size,(23,90,150)).save(buf,format='PNG');return buf.getvalue()


def encrypt(data, prefix=16, tail=10, xor=0x88):
    key=b'0123456789abcdef';pad=16-prefix%16
    encrypted=AES.new(key,AES.MODE_ECB).encrypt(data[:prefix]+bytes([pad])*pad)
    return imgdec.SIG+prefix.to_bytes(4,'little')+tail.to_bytes(4,'little')+b'\x00'+encrypted+data[prefix:len(data)-tail]+bytes(b^xor for b in data[-tail:]),key


class Format(unittest.TestCase):
    def test_png_middle_and_variable_xor_are_preserved_exactly(self):
        original=png((80,70))
        for prefix in (16,31,32):
            for xor in (0x88,0xD9,0x25):
                data,key=encrypt(original,prefix,10,xor)
                self.assertEqual(imgdec.decrypt_dat(data,key),original)

    def test_corrupt_padding_and_truncated_prefix_are_rejected(self):
        data,key=encrypt(png())
        self.assertIsNone(imgdec.decrypt_dat(data[:25],key))
        self.assertIsNone(imgdec.decrypt_dat(data,b'wrongkey00000000'))

    def test_no_xor_tail_and_plain_middle(self):
        original=png();key=b'0123456789abcdef';prefix=16
        first=AES.new(key,AES.MODE_ECB).encrypt(original[:prefix]+b'\x10'*16)
        data=imgdec.SIG+prefix.to_bytes(4,'little')+(0).to_bytes(4,'little')+b'\0'+first+original[prefix:]
        self.assertEqual(imgdec.decrypt_dat(data,key),original)

    def test_harvest_entrypoints_cannot_issue_ui_commands(self):
        with patch('core.docker_wx._exec',forbidden):
            self.assertEqual(harvest.harvest('synthetic',log=lambda *_:None),0)
            self.assertFalse(harvest.pull_latest_fullres('synthetic'))
            self.assertEqual(harvest._harvest_locked('synthetic',60,lambda *_:None),0)
            self.assertFalse(imgdec._fullres_enabled())
            imgdec.schedule_fullres('synthetic',1)


class Queue(Isolated):
    def rows(self):return [dict(local_id=1,type=3,content='synthetic')]

    def test_pending_media_becomes_ready_without_new_message_or_model(self):
        local_media.observe('chat-A',self.rows())
        with patch.object(imgdec,'get_msg_image',return_value=(None,'no-dat')):
            local_media.tick()
        self.assertEqual(local_media.observe('chat-A',self.rows())[1]['status'],'pending')
        with local_media.database() as con:con.execute('UPDATE media SET next_check=0')
        with patch.object(imgdec,'get_msg_image',return_value=(png((800,600)),'image/png')):
            local_media.tick()
        state=local_media.observe('chat-A',self.rows())[1]
        self.assertEqual((state['status'],state['width'],state['height']),('ready',800,600))
        self.assertEqual(state['attempts'],2)

    def test_preview_keeps_waiting_and_can_upgrade(self):
        local_media.observe('chat-A',self.rows())
        with patch.object(imgdec,'get_msg_image',return_value=(png(),'image/png')):local_media.tick()
        self.assertEqual(local_media.observe('chat-A',self.rows())[1]['status'],'preview')
        with local_media.database() as con:con.execute('UPDATE media SET next_check=0')
        with patch.object(imgdec,'get_msg_image',return_value=(png((1000,800)),'image/png')):local_media.tick()
        self.assertEqual(local_media.observe('chat-A',self.rows())[1]['status'],'ready')

    def test_missing_key_and_corrupt_image_are_explicit(self):
        row=dict(chat='chat-A',id=1,type=3)
        with patch.object(imgdec,'get_msg_image',return_value=(None,'no-img-key')):
            self.assertEqual(local_media.inspect(row)['status'],'missing_key')
        with patch.object(imgdec,'get_msg_image',return_value=(b'invalid','image/png')):
            self.assertEqual(local_media.inspect(row)['status'],'failed')

    def test_status_survives_new_connection_but_never_crosses_accounts(self):
        local_media.observe('chat-A',self.rows())
        with patch.object(imgdec,'get_msg_image',return_value=(png(),'image/png')):local_media.tick()
        self.switch('account-B')
        with local_media.database() as con:self.assertEqual(con.execute('SELECT count(*) FROM media').fetchone()[0],0)
        self.switch('account-A')
        self.assertEqual(local_media.observe('chat-A',self.rows())[1]['status'],'preview')

    def test_repeated_polling_does_not_reset_failed_retry_budget(self):
        local_media.observe('chat-A',self.rows())
        with local_media.database() as con:con.execute('UPDATE media SET attempts=90,next_check=0')
        local_media.observe('chat-A',self.rows())
        with patch.object(local_media,'inspect',forbidden):self.assertEqual(local_media.tick(),0)
        self.assertEqual(local_media.observe('chat-A',self.rows(),reset=True)[1]['attempts'],0)

    def test_only_media_rows_are_queued(self):
        self.assertEqual(local_media.observe('chat-A',[dict(local_id=2,type=1)]),{})

    def test_video_cover_is_not_presented_as_full_video(self):
        row=dict(chat='chat-A',id=1,type=43)
        with patch.object(media,'_video_base',return_value='resource'),patch.object(media,'video_paths',return_value=[]),patch.object(media,'get_msg_video_thumb',return_value=(png(),'image/png')):
            self.assertEqual(local_media.inspect(row)['status'],'poster')

    def test_larger_captured_image_replaces_old_local_preview(self):
        d=Path(self.tmp.name)/'capture';d.mkdir();(d/'resource.png').write_bytes(png((900,700)))
        with patch.object(imgdec,'_stored_image',return_value=(png(),'image/png')),patch.object(imgdec,'_effective_basehash',return_value='resource'),patch.object(imgdec,'_msg_img_md5',return_value=None),patch.object(imgdec,'_dat_paths',return_value=[]),patch.object(imgdec,'img_key',return_value=None),patch.object(imgdec,'_capture_dir',return_value=str(d)):
            data,mime=imgdec.get_msg_image('chat-A',1)
        with Image.open(io.BytesIO(data)) as im:self.assertEqual(im.size,(900,700))
        self.assertEqual(mime,'image/png')

    def test_video_paths_reject_invalid_files_before_claiming_ready(self):
        d=Path(self.tmp.name)/'videos';(d/'month').mkdir(parents=True)
        path=d/'month'/'resource.mp4';path.write_bytes(b'incomplete')
        with patch.object(media,'_video_root',return_value=str(d)),patch.object(imgdec,'_capture_dir',return_value=None):
            self.assertEqual(media.video_paths('resource'),[])
            path.write_bytes(b'\x00\x00\x00\x18ftypmp42'+b'\x00'*20)
            self.assertEqual(media.video_paths('resource'),[str(path)])

    def test_expired_hook_heartbeat_does_not_claim_ready(self):
        d=Path(self.tmp.name)/'capture';(d/'account-A').mkdir(parents=True)
        (d/'status.json').write_text(json.dumps({'state':'ready','updated_at':0,'captures':3}))
        with patch.object(imgdec,'_capture_dir',return_value=str(d/'account-A')):
            self.assertEqual(local_media.hook_status()['state'],'unavailable')


class Routes(Isolated):
    def test_old_pages_cannot_enable_navigation_through_any_media_endpoint(self):
        import server
        client=server.app.test_client()
        with patch.object(harvest,'harvest',forbidden),patch.object(server,'_ensure_focus_loop',forbidden),patch.object(server.threading,'Thread',forbidden):
            for path,body in [('/api/harvest',{'chat':'chat-A'}),('/api/bot/fullres',{'enabled':True}),('/api/focus',{'chat':'chat-A'})]:
                result=client.post(path,json=body)
                self.assertEqual(result.status_code,200)
                self.assertFalse(result.json['ui_navigation'])


if __name__=='__main__':
    unittest.main()
