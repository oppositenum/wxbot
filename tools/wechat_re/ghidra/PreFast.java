import ghidra.app.script.GhidraScript;
public class PreFast extends GhidraScript {
  @Override public void run() throws Exception {
    String[] off={"Decompiler Parameter ID","Aggressive Instruction Finder","DWARF","Decompiler Switch Analysis","Stack","Embedded Media","Non-Returning Functions - Discovered","Condense Filler Bytes","Variadic Function Signature Override","Demangler GNU"};
    for(String o:off){ try{ setAnalysisOption(currentProgram,o,"false"); println("off: "+o);}catch(Exception e){println("skip: "+o+" "+e.getMessage());} }
  }
}
