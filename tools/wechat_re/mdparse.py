"""解析微信 crashpad 的 minidump(aarch64):崩溃线程、异常地址、寄存器、栈上的 wechat 返回地址。

转储位置(容器内): /home/wechat/.xwechat/crashinfo/completed/*.dmp ,按时间取最新的。
地址显示为 模块!偏移,可直接拿偏移去 Ghidra/fnstart 查。
崩溃 pc 落在 libfrida-agent 里 = 我们的 hook 回调出事(多半是协程栈溢出),不是微信自己的 bug。

用法: python3 mdparse.py crash.dmp
"""
import struct,sys
d=open(sys.argv[1],'rb').read()
sig,ver,nstreams,dirrva=struct.unpack_from('<4sIII',d,0)
streams={}
for i in range(nstreams):
    t,sz,rva=struct.unpack_from('<III',d,dirrva+12*i); streams.setdefault(t,[]).append((sz,rva))
def mdstr(rva):
    n=struct.unpack_from('<I',d,rva)[0]; return d[rva+4:rva+4+n].decode('utf-16le','replace')
mods=[]
if 4 in streams:
    sz,rva=streams[4][0]; n=struct.unpack_from('<I',d,rva)[0]
    for i in range(n):
        o=rva+4+108*i; base,size=struct.unpack_from('<QI',d,o); nm=struct.unpack_from('<I',d,o+20)[0]
        mods.append((base,size,mdstr(nm).split('/')[-1]))
def sym(a):
    for b,s,n in mods:
        if b<=a<b+s: return '%s!0x%x'%(n,a-b)
    return hex(a)
sz,rva=streams[6][0]
tid,_,code,flags,rec,addr=struct.unpack_from('<IIIIQQ',d,rva)
print('crash tid',tid,'code',hex(code),'addr',hex(addr),sym(addr))
ctxsz,ctxrva=struct.unpack_from('<II',d,rva+8+152)
# ARM64 context (crashpad MinidumpContextARM64): flags(u32) cpsr(u32) regs[33] (x0..x28,fp,lr,sp,pc)
cf,cpsr=struct.unpack_from('<II',d,ctxrva); regs=struct.unpack_from('<33Q',d,ctxrva+8)
pc=regs[32]; sp=regs[31]; lr=regs[30]
print('pc',sym(pc),'lr',sym(lr),'sp',hex(sp))
for i in range(0,29): print('  x%d=%s'%(i,sym(regs[i])),end='\n' if i%4==3 else '')
print()
# stack scan for return addresses in wechat
if 5 in streams:
    sz,rva=streams[5][0]; n=struct.unpack_from('<I',d,rva)[0]
    for i in range(n):
        o=rva+4+16*i; start,dsz,drva=struct.unpack_from('<QII',d,o)
        if start<=sp<start+dsz:
            buf=d[drva:drva+dsz]; off=sp-start; out=[]
            for j in range(off,min(len(buf),off+0x1000),8):
                v=struct.unpack_from('<Q',buf,j)[0]; s=sym(v)
                if s.startswith('wechat!'): out.append(s)
            print('stack wechat ptrs:',out[:40])
