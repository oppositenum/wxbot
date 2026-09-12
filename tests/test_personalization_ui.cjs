// Offline DOM-control smoke test. No browser, network, models, or WeChat.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('static/personalization.html','utf8');
class El{
  constructor(tag=''){this.tag=tag;this.style={};this.children=[];this.value='';this.textContent='';this.hidden=false;this.checked=false;this.selectedOptions=[];}
  append(...xs){this.children.push(...xs)} prepend(...xs){this.children.unshift(...xs)}
  replaceChildren(...xs){this.children=[...xs]} add(x){this.children.push(x)}
}
const ids={};for(const x of html.matchAll(/id="([^"]+)"/g))ids['#'+x[1]]=new El();
const session={account:'synthetic-account',generation:'epoch-1'};
let activeSession=session,revision=0,settings={persona_id:null,preferences:{},personalization_enabled:true,auto_update:true,revision:0};
const calls=[];
const context={document:{querySelector:s=>ids[s],createElement:t=>new El(t)},Option:class extends El{constructor(label,value){super('option');this.textContent=label;this.value=value}},confirm:()=>true,
  fetch:async(url,opts={})=>{
    const body=opts.body?JSON.parse(opts.body):null;calls.push({url,body});if(url==='/api/admin/access')return {ok:true,json:async()=>({local_admin:false})};
    assert(url.startsWith('/api/personalization'),'unexpected network endpoint');
    let data={session:activeSession};
    if(url==='/api/personalization')Object.assign(data,{contacts:[{username:'friend-A',name:'同名'},{username:'friend-B',name:'同名'}],personas:[{slug:'P',name:'角色 P'}],global_config:{revision:0},legacy:{entries:[],unique:'P'},fields:{length:['未知','简短','详细'],tone:['未知','直接','温和']},labels:{length:'长度',tone:'语气'},jobs:[],default_template:{revision:1,persona_id:"P",template_enabled:true,template_source:"friend-A",preferences:{tone:{value:"温和"}}}});
    else if(url==='/api/personalization/template'){assert.equal(body.contact,'friend-A');assert.equal(body.revision,1);assert.deepEqual(body.session,session);}
    else if(url==='/api/personalization/template/apply'){assert.equal(body.contact,'friend-A');assert.equal(body.template_revision,1);assert.equal(body.revision,revision);assert.deepEqual(body.session,session);}
    else if(url.startsWith('/api/personalization/contact?'))Object.assign(data,{config:settings,effective:{name:'角色 P',source:'legacy_global'},audit:[]});
    else if(url==='/api/personalization/contact'&&body){assert.deepEqual(body.session,session);revision++;settings={...settings,...body.patch,revision};Object.assign(data,{config:settings});}
    else if(url==='/api/personalization/history/preview')Object.assign(data,{contact_count:1,model_calls:0,previews:[{contact:'friend-A',lo:0,hi:2,total:2,estimated_calls:0,proposals:[]}]});
    else if(url==='/api/personalization/preview')Object.assign(data,{context:['redacted'],model_calls:0,sends:0});
    else throw Error('unexpected endpoint '+url);
    return {ok:true,json:async()=>data};
  }
};
vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
(async()=>{
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/admin/access','startup only checks local access');
  ids['#token'].value='synthetic-token';await ids['#unlock'].onclick();
  assert.equal(calls.length,2);assert(!calls.some(c=>c.url.includes('history')));
  ids['#contacts'].value='friend-A';await ids['#contacts'].onchange();
  assert.equal(ids['#prefs'].children.length,2);assert(ids['#effective'].textContent.includes('待显式迁移'));
  ids['#persona'].value='P';await ids['#saveSettings'].onclick();
  assert.equal(revision,1);assert.equal(settings.persona_id,'P');
  ids['#query'].value='请详细说';await ids['#preview'].onclick();assert(ids['#previewResult'].textContent.includes('redacted'));
  ids['#batchContacts'].selectedOptions=[{value:'friend-A'}];ids['#limit'].value='2';await ids['#scan'].onclick();
  assert.equal(calls.filter(c=>c.url.includes('/history/preview')).length,1);
  assert.equal(calls.filter(c=>c.url.includes('/history/create')||c.url.includes('/history/step')).length,0);
  ids['#templateSource'].value='friend-A';await ids['#saveTemplate'].onclick();assert(calls.some(c=>c.url==='/api/personalization/template'));
  ids['#contacts'].value='friend-A';await ids['#contacts'].onchange();assert.equal(ids['#applyTemplate'].hidden,false);await ids['#applyTemplate'].onclick();assert(calls.some(c=>c.url==='/api/personalization/template/apply'));
  activeSession={account:'synthetic-account-B',generation:'epoch-2'};await ids['#refreshJobs'].onclick();
  assert.equal(ids['#workspace'].hidden,true);assert(ids['#error'].textContent.includes('账号已变化'));
  const writes=calls.filter(c=>c.url==='/api/personalization/contact').length;
  await ids['#saveSettings'].onclick();assert.equal(calls.filter(c=>c.url==='/api/personalization/contact').length,writes);
  console.log('PASS: 7 offline UI flow checks (load, inheritance display, save, preview, bounded scan, account switch, stale form blocked). Real browser rendering remains unverified.');
})().catch(e=>{console.error(e);process.exitCode=1});
