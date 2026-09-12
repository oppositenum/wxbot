const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('static/accounts.html','utf8'),els={};
for(const [,id] of html.matchAll(/id="([^"]+)"/g))els['#'+id]={value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,replaceChildren(){this.innerHTML=''},close(){},showModal(){}};
let tick,calls=[],allow=false;
const c={document:{querySelector:s=>els[s],querySelectorAll:()=>[]},setInterval:fn=>{tick=fn},confirm:()=>true,fetch:async(url,opt)=>{calls.push({url,opt});return {status:allow?200:403,ok:allow,json:async()=>allow?{accounts:[]}:{error:'授权失败'}}}};
vm.createContext(c);vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],c);
(async()=>{
 tick();assert.equal(calls.length,0);assert.equal(els['#add'].disabled,true);
 els['#adminToken'].value='wrong';await els['#authForm'].onsubmit({preventDefault(){}});assert.equal(calls.length,1);assert.equal(els['#add'].disabled,true);assert.equal(els['#adminToken'].value,'');tick();assert.equal(calls.length,1);
 allow=true;els['#adminToken'].value='synthetic-token';await els['#authForm'].onsubmit({preventDefault(){}});assert.equal(els['#add'].disabled,false);assert.equal(calls.at(-1).opt.headers['X-Wxbot-Admin-Token'],'synthetic-token');
 await els['#refresh'].onclick();assert.equal(calls.at(-1).opt.headers['X-Wxbot-Admin-Token'],'synthetic-token');
 allow=false;await els['#refresh'].onclick();assert.equal(els['#add'].disabled,true);let n=calls.length;tick();assert.equal(calls.length,n);
 els['#logout'].onclick();assert.equal(els['#grid'].innerHTML,'');
 console.log('PASS: no unauthenticated polling, wrong-token error, header forwarding, authorized refresh, expired-token lock and logout');
})().catch(e=>{console.error(e);process.exit(1)});
