#!/usr/bin/env python3
"""Export/import wxbot dashboard configuration.

Usage:
  python3 tools/wxbot-config.py export -o wxbot-config.zip
  python3 tools/wxbot-config.py import wxbot-config.zip

The archive contains account settings, personas, profiles, schedules,
knowledge and moments data. Secrets, keys, decrypted WeChat databases and
runtime state are deliberately excluded.
"""
import argparse, json, os, shutil, tempfile, time, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(ROOT, "accounts")
EXCLUDE_NAMES = {"keys.json", "llm_config.json", "decrypted", "msg", "mediacache", "send_ledger.sqlite3"}
EXCLUDE_SUFFIXES = (".db-wal", ".db-shm")

def allowed(rel):
    parts = rel.split(os.sep)
    if any(p in EXCLUDE_NAMES for p in parts): return False
    if any(rel.endswith(s) for s in EXCLUDE_SUFFIXES): return False
    return True

def collect_files():
    if not os.path.isdir(ACCOUNTS): return []
    out=[]
    for base, dirs, files in os.walk(ACCOUNTS):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
        for fn in files:
            full=os.path.join(base, fn); rel=os.path.relpath(full, ROOT)
            if allowed(rel): out.append(rel)
    return sorted(out)

def export(path):
    files=collect_files()
    manifest={"format":"wxbot-config-v1", "created_at":int(time.time()),
              "files":files, "excluded":["keys.json","llm_config.json","decrypted/","msg/","mediacache/"]}
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("wxbot-manifest.json", json.dumps(manifest,ensure_ascii=False,indent=2))
        for rel in files: z.write(os.path.join(ROOT,rel),rel)
    print(f"已导出 {len(files)} 个配置文件：{os.path.abspath(path)}")
    print("已排除微信登录态、keys.json、llm_config.json、解密数据库和运行时缓存。")

def import_archive(path):
    if not os.path.isfile(path): raise SystemExit(f"找不到导入文件：{path}")
    with zipfile.ZipFile(path) as z:
        try: manifest=json.loads(z.read("wxbot-manifest.json"))
        except Exception: raise SystemExit("不是有效的 wxbot 配置包")
        if manifest.get("format") != "wxbot-config-v1": raise SystemExit("不支持的配置包版本")
        names=[n for n in z.namelist() if n != "wxbot-manifest.json"]
        if any(n.startswith("/") or ".." in n.split("/") or not allowed(n) for n in names):
            raise SystemExit("配置包包含非法或受保护路径")
        os.makedirs(ACCOUNTS,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wxbot-import-") as td:
            for n in names:
                dest=os.path.join(td,n); os.makedirs(os.path.dirname(dest),exist_ok=True)
                with z.open(n) as src, open(dest,"wb") as dst: shutil.copyfileobj(src,dst)
            for n in names:
                src=os.path.join(td,n); dest=os.path.join(ROOT,n)
                os.makedirs(os.path.dirname(dest),exist_ok=True)
                shutil.copy2(src,dest)
    print(f"已导入 {len(names)} 个配置文件。请重启 5100 后台使配置生效。")
    print("导入不会覆盖微信登录态、keys.json 或 llm_config.json。")

def main():
    p=argparse.ArgumentParser(description="wxbot 5100 配置迁移工具")
    sub=p.add_subparsers(dest="cmd",required=True)
    e=sub.add_parser("export"); e.add_argument("-o","--output",required=True)
    i=sub.add_parser("import"); i.add_argument("archive")
    a=p.parse_args()
    export(a.output) if a.cmd=="export" else import_archive(a.archive)
if __name__=="__main__": main()
