# Webhook 订阅：把收到的消息加密推给你的系统

机器人收到消息后，会把消息 **加密** 后 POST 到你提供的地址。每个订阅有自己的 **密钥**（64 位十六进制），
只有拿着密钥的一方能解开内容，也能确认消息确实来自这个机器人、没被改过。

在控制台「对外接口 → Webhook 订阅」里新建订阅，把 **接收地址** 和 **密钥** 交给对接方。

- 机器人要处于「运行中」才会推送。
- 可以按会话（全部 / 选定）、私聊 / 群、消息类型过滤；默认不推自己发的消息。
- 投递失败（非 2xx、超时 8 秒、连不上）会在 10 秒、60 秒后各重试一次。

## 请求格式

```
POST <你的地址>
Content-Type: application/json
User-Agent: wxbot-webhook/1
X-Wxbot-Event: message            # 或 test(在控制台点「测试」时)
X-Wxbot-Id: evt_3f9c…             # 事件 id:同一条消息重试时不变,用来去重
X-Wxbot-Timestamp: 1732800000     # 发送时间(秒)

{"v":1,"alg":"AES-256-GCM","id":"evt_3f9c…","ts":1732800000,
 "iv":"<base64,12 字节>","data":"<base64,密文 + 16 字节认证标签>"}
```

返回任意 **2xx** 表示收到（建议先返回再慢慢处理，超过 8 秒算失败）。

## 解密

| 项 | 值 |
| --- | --- |
| 算法 | AES-256-GCM |
| 密钥 | `hex_decode(密钥)`，32 字节 |
| IV / nonce | `base64_decode(iv)`，12 字节 |
| 附加数据 AAD | 字符串 `"{id}.{ts}"`，例如 `evt_3f9c….1732800000`（UTF-8） |
| 密文 | `base64_decode(data)`，**最后 16 字节是认证标签** |

解密失败（密钥不对、内容被改、id/ts 被改）一律丢弃。建议同时：
- 丢弃 `ts` 和当前时间相差超过 5 分钟的请求（防重放）；
- 记住最近处理过的 `id`，重复的直接返回 200（重试可能重复送达）。

## 解开后的内容

```json
{
  "event": "message",
  "account": {"wxid": "wxid_机器人自己"},
  "message": {
    "chat": "wxid_xxx 或 123@chatroom",
    "chat_name": "会话名(备注/群名)",
    "is_group": false,
    "sender": "发消息的人的 wxid(群里有,私聊可能为空)",
    "sender_name": "发消息的人的名字",
    "is_self": false,
    "type": 1,
    "category": "text",
    "category_name": "文字",
    "content": "原始内容(文字就是文字;其他类型是 [图片] 这类占位或 XML)",
    "summary": "一行可读摘要,例如 [转账] ¥88.00 房租",
    "meta": {"amount": "88.00"},
    "time": "2026-11-29 10:20:30",
    "timestamp": 1732846830,
    "local_id": 1234,
    "server_id": "…"
  }
}
```

`category` 取值：text 文字、image 图片、voice 语音、video 视频、sticker 表情、location 位置、card 名片、file 文件、
link 链接、miniapp 小程序、video_channel 视频号、merged 聊天记录、red_packet 红包、transfer 转账、voip 音视频通话、
quote 引用、system 系统、revoke 撤回、other 其他。超大的消息（> 256 KB）content 会被截断并带 `"truncated": true`。

## 示例代码

### Python（`pip install cryptography`）

```python
import base64, json, time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET = "你的密钥(64 位十六进制)"

def decrypt(body: bytes) -> dict:
    env = json.loads(body)
    if abs(time.time() - int(env["ts"])) > 300:
        raise ValueError("过期")
    aad = f'{env["id"]}.{env["ts"]}'.encode()
    plain = AESGCM(bytes.fromhex(SECRET)).decrypt(
        base64.b64decode(env["iv"]), base64.b64decode(env["data"]), aad)
    return json.loads(plain)

# Flask 接收:
# @app.post("/wx-hook")
# def hook():
#     msg = decrypt(request.get_data())
#     print(msg["message"]["chat_name"], msg["message"]["summary"])
#     return "ok"
```

### Node.js（无需第三方库）

```js
const crypto = require('crypto');
const SECRET = '你的密钥(64 位十六进制)';

function decrypt(body) {               // body: 原始请求体字符串
  const env = JSON.parse(body);
  if (Math.abs(Date.now() / 1000 - env.ts) > 300) throw new Error('过期');
  const raw = Buffer.from(env.data, 'base64');
  const d = crypto.createDecipheriv('aes-256-gcm', Buffer.from(SECRET, 'hex'), Buffer.from(env.iv, 'base64'));
  d.setAAD(Buffer.from(`${env.id}.${env.ts}`));
  d.setAuthTag(raw.subarray(raw.length - 16));
  const plain = Buffer.concat([d.update(raw.subarray(0, raw.length - 16)), d.final()]);
  return JSON.parse(plain.toString('utf8'));
}

// Express 接收:
// app.post('/wx-hook', express.text({type: '*/*'}), (req, res) => {
//   const msg = decrypt(req.body); console.log(msg.message.summary); res.send('ok');
// });
```

### Go（标准库）

```go
package main

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"
)

const secret = "你的密钥(64 位十六进制)"

type envelope struct {
	ID   string `json:"id"`
	TS   int64  `json:"ts"`
	IV   string `json:"iv"`
	Data string `json:"data"`
}

func decrypt(body []byte) (map[string]any, error) {
	var env envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, err
	}
	if math.Abs(float64(time.Now().Unix()-env.TS)) > 300 {
		return nil, errors.New("过期")
	}
	key, _ := hex.DecodeString(secret)
	iv, _ := base64.StdEncoding.DecodeString(env.IV)
	data, _ := base64.StdEncoding.DecodeString(env.Data)
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, _ := cipher.NewGCM(block)
	plain, err := gcm.Open(nil, iv, data, []byte(fmt.Sprintf("%s.%d", env.ID, env.TS)))
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal(plain, &out)
}
```

### Java（JDK 自带）

```java
import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HexFormat;

public class WxHook {
    static final String SECRET = "你的密钥(64 位十六进制)";

    /** id / ts / iv / data 从请求 JSON 里取;返回解开的 JSON 字符串。 */
    static String decrypt(String id, long ts, String iv, String data) throws Exception {
        if (Math.abs(System.currentTimeMillis() / 1000 - ts) > 300) throw new SecurityException("过期");
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(HexFormat.of().parseHex(SECRET), "AES"),
               new GCMParameterSpec(128, Base64.getDecoder().decode(iv)));
        c.updateAAD((id + "." + ts).getBytes(StandardCharsets.UTF_8));
        return new String(c.doFinal(Base64.getDecoder().decode(data)), StandardCharsets.UTF_8);
    }
}
```

### PHP（openssl 扩展）

```php
<?php
const SECRET = '你的密钥(64 位十六进制)';

function wx_decrypt(string $body): array {
    $env = json_decode($body, true);
    if (abs(time() - (int)$env['ts']) > 300) throw new Exception('过期');
    $raw = base64_decode($env['data']);
    $plain = openssl_decrypt(substr($raw, 0, -16), 'aes-256-gcm', hex2bin(SECRET), OPENSSL_RAW_DATA,
                             base64_decode($env['iv']), substr($raw, -16), $env['id'] . '.' . $env['ts']);
    if ($plain === false) throw new Exception('解密失败');
    return json_decode($plain, true);
}

// $msg = wx_decrypt(file_get_contents('php://input')); echo 'ok';
```

## 密钥管理

- 新建订阅时页面会显示一次完整密钥；之后可以在订阅上点「查看密钥」（需要登录控制台）。
- 怀疑泄露就点「换密钥」：旧密钥立刻失效，对接方要换成新的。
- 密钥存在微信容器的账号目录 `webhooks.json`（权限 600），不会进「配置导入导出」的包。
