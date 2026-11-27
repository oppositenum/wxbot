// args: outfile addr1 addr2 ... ; for each addr: containing function, decompiled C, callers, callees
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import java.io.*;
import java.util.*;
public class DumpFns extends GhidraScript {
  @Override public void run() throws Exception {
    String[] a=getScriptArgs(); PrintWriter w=new PrintWriter(new FileWriter(a[0]));
    DecompInterface d=new DecompInterface(); d.openProgram(currentProgram);
    AddressSpace sp=currentProgram.getAddressFactory().getDefaultAddressSpace();
    long base=currentProgram.getImageBase().getOffset();
    for(int i=1;i<a.length;i++){
      Address ad=sp.getAddress(base+Long.decode(a[i]));
      Function f=getFunctionContaining(ad);
      w.println("==== "+a[i]+" -> "+(f==null?"NO FUNCTION":f.getName()+" @0x"+Long.toHexString(f.getEntryPoint().getOffset()-base)+" size "+f.getBody().getNumAddresses()));
      if(f==null){
        for(Reference r:getReferencesTo(ad)) w.println("  ref from 0x"+Long.toHexString(r.getFromAddress().getOffset()-base)+" "+r.getReferenceType());
        continue;}
      Set<String> callers=new TreeSet<>();
      for(Reference r:getReferencesTo(f.getEntryPoint())){Function c=getFunctionContaining(r.getFromAddress());callers.add("0x"+Long.toHexString(r.getFromAddress().getOffset()-base)+(c==null?"":" in "+c.getName()));}
      w.println("  callers("+callers.size()+"): "+callers);
      Set<String> callees=new TreeSet<>(); for(Function c:f.getCalledFunctions(monitor)) callees.add(c.getName()+"@0x"+Long.toHexString(c.getEntryPoint().getOffset()-base));
      w.println("  callees("+callees.size()+"): "+callees);
      DecompileResults r=d.decompileFunction(f,120,monitor);
      w.println(r.decompileCompleted()?r.getDecompiledFunction().getC():"  DECOMPILE FAILED "+r.getErrorMessage());
      w.flush();
    }
    w.close();
  }
}
