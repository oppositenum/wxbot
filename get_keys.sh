#!/bin/bash
# 提取微信数据库密钥（lldb 断点 CommonCrypto CCCryptorCreate 抓 AES-256 key）。
# 需要：SIP 已关闭 + sudo。运行期间请在微信里点开「聊天」「通讯录」并进入几个会话/联系人，
# 以触发 message_0.db / contact.db / session.db / head_image.db 的解密。
#
# 用法： sudo ./get_keys.sh [持续秒数，默认 60]
set -e
cd "$(dirname "$0")"

PID=$(pgrep -x WeChat | head -1)
if [ -z "$PID" ]; then echo "微信未运行"; exit 1; fi
DUR=${1:-60}
echo "WeChat pid=$PID，抓取 ${DUR}s。请在此期间在微信里点「聊天」「通讯录」并进入几个会话/联系人。"

lldb -b \
  -o "process attach -p $PID" \
  -o "command script import core/cc_catch.py" \
  -o "cont" > /tmp/lldb.log 2>&1 &
LLDB=$!
osascript -e 'tell application "WeChat" to activate' 2>/dev/null || true
sleep "$DUR"
kill "$LLDB" 2>/dev/null || true
pkill -f "lldb.*process attach" 2>/dev/null || true
sleep 1

echo "抓到 $(sort -u /tmp/cc_keys.txt 2>/dev/null | wc -l | tr -d ' ') 个候选 key，开始校验匹配…"
python3 -m core.cc_validate
