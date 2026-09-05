"""并行全内存扫描 raw key：主进程(root)读内存，worker 池做 numpy 过滤 + AES 预检。
用法： sudo WXBOT_STEP=4 python3 -m core.parscan
"""
import os, sys, json, time, collections
import numpy as np
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from core import keys as K

STEP = int(os.environ.get("WXBOT_STEP", "4"))
NPROC = int(os.environ.get("WXBOT_NPROC", "8"))
_PAGES = None  # [(rel, page1)]

def _winit(pages):
    global _PAGES
    _PAGES = pages

def _cand_offsets(arr):
    L = len(arr)
    if L < 32: return np.empty(0, np.int64)
    offs = np.arange(0, L-31, STEP, dtype=np.int64)
    z = (arr==0).astype(np.int32)
    p = ((arr>=0x20)&(arr<0x7f)).astype(np.int32)
    zp=np.empty(L+1,np.int32); zp[0]=0; np.cumsum(z,out=zp[1:])
    pp=np.empty(L+1,np.int32); pp[0]=0; np.cumsum(p,out=pp[1:])
    zc=zp[offs+32]-zp[offs]; pc=pp[offs+32]-pp[offs]
    return offs[(zc<5)&(pc<=24)]

def _scan(job):
    base, buf = job
    arr = np.frombuffer(buf, np.uint8)
    offs = _cand_offsets(arr)
    hits=[]
    for o in offs.tolist():
        cand = buf[o:o+32]
        for rel, page in _PAGES:
            if K.precheck_key(cand, page):
                hits.append((rel, base+o, cand.hex()))
    return len(offs), hits

def main():
    pid=K.wechat_pid(); task=K.task_for_pid(pid)
    db=config.db_storage_dir(); meta=K.load_db_meta(db)
    core=[v for v in config.CORE_DBS.values() if v in meta]
    pages=[(r, meta[r]['page1']) for r in core]
    print(f"pid={pid} step={STEP} nproc={NPROC} 目标 {len(core)} 库", flush=True)
    keys={}
    def window_brute(center):
        radius=8*1024*1024; lo=max(0,center-radius)
        d=K.read_mem(task, lo, radius*2)
        if not d: return
        start=(-lo)%16
        for o in range(start, len(d)-31, 4):
            for r in core:
                if r in keys: continue
                if K.precheck_key(d[o:o+32], meta[r]['page1']):
                    keys[r]=d[o:o+32].hex(); print("  ✓(win)",r,keys[r][:16],flush=True)

    alltags = bool(os.environ.get("WXBOT_ALLTAGS"))
    def jobs():
        CH=8*1024*1024; OV=32
        for addr,size,prot,tag in K.iter_regions(task):
            if not (prot&K.VM_PROT_READ) or size>K.MAX_REGION: continue
            if not alltags and tag==0: continue
            off=0
            while off<size:
                n=min(CH,size-off); d=K.read_mem(task,addr+off,min(n+OV,size-off)); base=addr+off; off+=n
                if d: yield (base,d)

    t0=time.time(); tot_c=0; tot_b=0; done=0
    with Pool(NPROC, initializer=_winit, initargs=(pages,)) as pool:
        for ncand, hits in pool.imap_unordered(_scan, jobs(), chunksize=1):
            done+=1; tot_c+=ncand
            for rel,addr,hx in hits:
                if rel not in keys:
                    keys[rel]=hx; print("  ✓",rel,hx[:16],"@",hex(addr),flush=True)
                    window_brute(addr)
            if len(keys)>=len(core):
                print("  全部命中，停止。",flush=True); pool.terminate(); break
            if done%200==0:
                print(f"  …{done}块 候选{tot_c} 命中{len(keys)}/{len(core)} {time.time()-t0:.0f}s",flush=True)

    out={r:keys[r] for r in keys}
    with open(config.keys_json(),"w") as f: json.dump(out,f,indent=2)
    su=os.environ.get("SUDO_USER")
    if su and su!="root":
        import pwd; pw=pwd.getpwnam(su)
        try: os.chown(config.keys_json(), pw.pw_uid, pw.pw_gid)
        except OSError: pass
    print(f"[done] 命中 {len(keys)}/{len(core)}: {list(keys)}  {time.time()-t0:.0f}s",flush=True)

if __name__=="__main__":
    main()
