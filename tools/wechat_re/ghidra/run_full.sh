#!/bin/bash
# Ghidra headless 全量分析微信主程序(130MB,8 核约 45 分钟)。结果存成项目,之后用 query.sh 秒查。
# 需要:Ghidra 12.x + JDK 21。PreFast.java 关掉 10 个最慢且对本任务无用的分析器,否则跑不完。
#
#   GHIDRA_HOME=~/.local/rea/engines/ghidra_12.1.4_PUBLIC \
#   JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
#   ./run_full.sh /path/to/wechat ~/wxbot-re/ghidra/proj
#
# 跑完可设 NOTIFY_URL 推送通知(可选,不设就不推)。
set -u
BIN=${1:?wechat 二进制路径}
PROJ=${2:?Ghidra 项目目录}
HERE=$(cd "$(dirname "$0")" && pwd)
: "${GHIDRA_HOME:?设置 GHIDRA_HOME}"
: "${JAVA_HOME:?设置 JAVA_HOME(JDK 21)}"
export PATH=$JAVA_HOME/bin:$PATH GHIDRA_MAXMEM=${GHIDRA_MAXMEM:-10G}
mkdir -p "$PROJ"
LOG=$PROJ/full.log
start=$(date +%s)
"$GHIDRA_HOME/support/analyzeHeadless" "$PROJ" wechat_full -import "$BIN" \
  -scriptPath "$HERE" -preScript PreFast.java \
  -analysisTimeoutPerFile 36000 -max-cpu "${MAX_CPU:-8}" > "$LOG" 2>&1
rc=$?
mins=$(( ($(date +%s)-start)/60 ))
echo "DONE rc=$rc mins=$mins" >> "$LOG"
[ -n "${NOTIFY_URL:-}" ] && curl -s -G "$NOTIFY_URL" --data-urlencode "msg=Ghidra 全量分析结束 rc=$rc ${mins}min" -o /dev/null
exit $rc
