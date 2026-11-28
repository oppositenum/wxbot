# AI 语音通话（实验）

`tools/voice_call.py`：用微信语音通话拨给一个人，接通后 AI 实时对话。目前只允许拨给测试号 🐮🐮🌱besos。

```bash
docker exec -u wechat wxbot-ubuntu-manual sh -c 'cd /app && python3 tools/voice_call.py --selftest'       # 不打电话，测延迟
docker exec -u wechat wxbot-ubuntu-manual sh -c 'cd /app && python3 tools/voice_call.py --dial --chat wxid_vn2w1t3doieu22'
```

拨号前要在主窗口打开对方的聊天（程序会 OCR 核对标题，不对就不拨）。

## 怎么做的

| 环节 | 做法 |
| --- | --- |
| 拨号 / 接通 / 挂断 | xdotool 点聊天右上角电话 → 「语音通话」；通话窗口 OCR 到计时 `00:00` 算接通（或先听到对方说话）；挂断点通话窗口底部 |
| 我们说话 | 合成的声音播进 PulseAudio `vmic` → `virtual_mic`（微信的麦克风） |
| 听对方 | 默认输出改成 `vspk`，录 `vspk.monitor` → 阿里云 `paraformer-realtime-v2` 实时识别（700ms 静音算一句） |
| 想 | 人设先浓缩成 300 字「通话角色卡」（按内容缓存），只带最近 8 条对话，AI 流式输出 |
| 说 | CosyVoice 流式合成（对方一停就提前建好合成会话，省 ~1.1 秒），边合成边播 |
| 轮次 | 不排队：攒下的话合成一句回；AI 还没开口对方又说话 → 取消、合并、重想；对方说 ≥3 个汉字就打断我们；过滤 Yeah/单字碎片和回声 |

每通电话的录音（`remote.wav`）、识别、回复和各环节耗时在 `work/voice_calls/<时间>/events.jsonl`。

## 实测（2026-11-29，三通）

| | 第 2 通 | 第 3 通 |
| --- | --- | --- |
| 对方说完 → 我们开口（中位 / 最慢） | 5.0 / 11.7 秒 | 4.1 / 5.2 秒 |
| 回答旧话 | 多次 | 0 |

坑：
- 第 1 通把自己的录音器也挪到了麦克风上，录的一直是自己 → 只挪微信自己的流。
- 「夏以昼」会被识别成「下宇宙」「下一周」→ 规则里提示 AI；后续可加识别热词。
- 自动打电话比发消息更容易触发风控，只在测试号之间用。
