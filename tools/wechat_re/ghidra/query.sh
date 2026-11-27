#!/bin/bash
# 查询已分析好的项目:每个地址输出所在函数、调用者、被调用者、反编译 C。
# 地址用模块偏移(镜像基址 0);Ghidra 内部基址是 0x100000,DumpFns 会自动换算。
#   ./query.sh ~/wxbot-re/ghidra/proj out.txt 0x4baee74 0x39dee1c
set -u
PROJ=${1:?Ghidra 项目目录}; shift
HERE=$(cd "$(dirname "$0")" && pwd)
: "${GHIDRA_HOME:?设置 GHIDRA_HOME}"
: "${JAVA_HOME:?设置 JAVA_HOME(JDK 21)}"
export PATH=$JAVA_HOME/bin:$PATH GHIDRA_MAXMEM=${GHIDRA_MAXMEM:-8G}
"$GHIDRA_HOME/support/analyzeHeadless" "$PROJ" wechat_full -process wechat -noanalysis -readOnly \
  -scriptPath "$HERE" -postScript DumpFns.java "$@" 2>&1 | grep -E "ERROR|Exception" | head -5
