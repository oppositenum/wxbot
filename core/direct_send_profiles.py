"""直发 agent 的按版本配置(偏移都是模块内偏移,Ghidra 名字要减 0x100000)。

core/direct_send.py 按微信二进制 sha256 选一套,注入给 core/direct_send_agent.js。
没有配置的版本一律禁用直发(偏移错了轻则闪退,重则往微信库里写乱码)。
过程与重新定位方法见 docs/wechat-direct-send-re.md。
"""

# 请求对象字段:one/seven 是两个固定值(界面发送实测),to 收件人,type 类型,sub 文件子类型,
# len 正文字节数或文件大小,content 正文,path 本地文件路径,filename 文件名(4.1.13 起),
# video_flags 视频额外的字节标志 [(偏移, 值)],atList 被 @ 的 wxid 列表(没有=不支持 @ 提醒)
PROFILES = {
    # ---- WeChat Linux 4.1.8 aarch64 ----
    "e40116597803a5e0d9fc4a2b727df4b8f7a3dd6de1c336140ea373be2b6626e9": {
        "name": "4.1.8 aarch64",
        "sendMessage": 0x4baee74, "svcA": 0x483104c, "svcB": 0x4950c40, "svcC": 0x496004c, "svcVt": 0x7f1fb80,
        "opNew": 0x7d7e420, "strAssign": 0x3639df8, "coLaunch": 0x49bf7bc, "appCtx": 0x82bb2a8,
        "execOff": 0x290, "typeInfo": 0x7f6a440,
        # make_shared 构造:块 = 控制块 0x18 + 对象,对象在 blk+0x18
        "req": {
            "style": "ctor",
            "text": {"make": 0x39deee4, "blk": 0x428},
            "image": {"make": 0x39dee1c, "blk": 0x468},
            "video": {"ctor": 0x40f95f8, "blk": 0x428, "ctrlGot": 0x80e5430},
            "file": {"make": 0x40f97dc, "blk": 0x3f8, "zero": True},
        },
        "fields": {"one": 0x6c, "seven": 0x70, "to": 0x78, "type": 0x94, "sub": 0x98, "len": 0xe8,
                   "content": 0x3e0, "path": 0xa0, "filename": None, "video_flags": [[0x3e0, 1]],
                   "objSize": 0x410},
        "voice": False,      # 4.1.8 没有发语音条的代码
        "revoke": {
            "revokeSvc": 0x4bbe8bc, "revokeSvcVt": 0x7f4b030, "revoke": 0x5081554, "msgByLocalId": 0x4bc9fe4,
        },
    },
    # ---- WeChat Linux 4.1.13.29 aarch64(2026-11-28 定位)----
    "3539690ff31b8b85af3bb84b8f1087edda9ce324db9fdab10f40d7622fba42d9": {
        "name": "4.1.13 aarch64",
        # 单条 SendMessage 仍在(140 字节,调核心 0x6306088(svc,&sp,1));界面改走批量 0x6311da4
        "sendMessage": 0x6305fe8, "svcA": 0x5eee3dc, "svcB": 0x602d068, "svcC": 0x603c5cc, "svcVt": 0xa1fdad8,
        "opNew": 0x9f9f2e0, "strAssign": 0x449bf38, "coLaunch": 0x60d093c, "appCtx": 0xa68ac00,
        "execOff": 0x290, "typeInfo": 0xa1da530,
        # 请求对象带 enable_shared_from_this,直接调微信的创建函数(x8 返回 shared_ptr),不手动构造
        "req": {
            "style": "maker",
            "text": {"maker": 0x4973de8},    # vt 0xa1da638,块 0x610
            "image": {"maker": 0x4973c48},   # vt 0xa1da688,块 0x658
            "video": {"maker": 0x53110a8},   # vt 0xa1da6d8,块 0x628
            "file": {"maker": 0x5246238},    # vt 0xa1da5e8(基类),块 0x5e0;工厂 0x530e40c 随后写 +0xe4/+0xe8 = 49/6
            # 语音:x8=输出, x1=&收件人 string, x2=&SILK 字节 string, x3=&时长毫秒(u32), x4=&附加信息
            # {u32 格式=4, u32 3, u64 30000, u8 0, string ""}(界面录音实测)。vt 0xa3143c8,块 0x630
            "voice": {"maker": 0x7db9548, "info": [4, 3, 30000]},
        },
        "fields": {"one": 0x7c, "seven": 0x84, "to": 0x90, "type": 0xe4, "sub": 0xe8, "len": 0x170,
                   "content": 0x5c8, "path": 0xf0, "filename": 0x158, "video_flags": [[0x5dc, 4]],
                   "voiceData": 0x5c8,      # 语音请求里 SILK 字节的 std::string(和文字正文同一偏移)
                   "atList": 0x5e0,         # 文字请求:被 @ 的 wxid,逗号分隔(界面发 @ 实测)
                   "objSize": 0x5f0},
        # 撤回(界面右键撤回实测,docs 3.16):
        #   GetMsgByLocalId 0x6313cd0(x8=Msg 输出, x0=消息服务, x1=&会话, w2=local_id, w3=create_time),找到标志 +0x278
        #   Msg → 消息行 0x5f09c2c(x8=行 0x350 字节, x0=const Msg&),行 vtable 0xa1d8860
        #   撤回服务:svcA → svcB → 0x6306de0(x8),vtable 0xa22d7e8
        #   批量撤回 0x6869584(x8=结果, x0=撤回服务, x1=&vector<行>)
        "revoke": {
            "style": "batch",
            "revokeSvc": 0x6306de0, "revokeSvcVt": 0xa22d7e8, "batch": 0x6869584,
            "msgByLocalId": 0x6313cd0, "msgFound": 0x278, "toRow": 0x5f09c2c, "rowSize": 0x350,
            "row": {"time": 0xa8, "localId": 0x74, "sortSeq": 0xa0, "displayName": 0x248},
        },
        # 引用回复 = 文字请求改成 appmsg 49/57,被引用消息的「消息行」放进 +0x1d8 的 optional(有值标志 +0x528),
        # 另有 +0x190 会话、+0x1a8 时间、+0x1ac local_id、+0x1b0 sort_seq、+0x1d0=1、+0x568 空串的 md5。
        # 微信按消息行自己拼 <refermsg>。消息行拷贝构造 0x46b2154(x0=目标, x1=源)。
        "quote": {
            "rowOff": 0x1d8, "rowSet": 0x528, "chat": 0x190, "time": 0x1a8, "localId": 0x1ac,
            "sortSeq": 0x1b0, "flag": 0x1d0, "md5": 0x568, "rowCopy": 0x46b2154, "type": 49, "sub": 57,
        },
        "voice": True,       # 4.1.9+ 才有语音条
    },
}
