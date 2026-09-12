// Offline DOM double: verify user actions, not browser rendering or model quality.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('static/personalization.html','utf8');
class El {
  constructor(tag=''){this.tag=tag;this.style={};this.children=[];this.value='';this.hidden=false;this.checked=false;this.textContent='';this.selectedOptions=[];}
  append(...a){this.children.push(...a)} prepend(...a){this.children.unshift(...a)} replaceChildren(...a){this.children=[...a]} add(x){this.children.push(x)}
}
const ids={};for(const x of html.matchAll(/id="([^"]+)"/g))ids['#'+x[1]]=new El();
const session={account:'test-account',generation:'1'},calls=[];
let approve=false,revision=0,itemStatus='pending',batchStatus='ready';
const setting=()=>({revision,preferences:{},persona_id:null,personalization_enabled:true,auto_update:true,conversation_control_enabled:true});
function detail(){return {session,job:{id:'job1',contact:'friend-A',start:1700000000,end:1700100000,status:'preview',contact_count:1,message_count:3,segments:2,sampled_segments:2,expected_calls:1,max_calls:2,input_token_estimate:4000,tokens_reserved:batchStatus==='done'?5800:0,token_budget:60000,calls_reserved:batchStatus==='done'?1:0,route:{provider:'gpt',model:'test-model'}},batches:[{ordinal:0,status:batchStatus,attempts:batchStatus==='done'?1:0,rows:[{id:'e1',message_id:'1',text:'合成证据',snippet:0,time:1700000000,source:'text'}]}],items:batchStatus==='done'?[{id:'item1',status:itemStatus,category:'expression',dimension:'message_shape',value:'分条短消息',scope:'chat:friend-A',rationale:'合成依据',confidence:'medium',sufficient:true,evidence_ids:['e1'],conflict_ids:[],locked_conflict:false,application_id:'apply1'}]:[],current:setting()};}
const env={document:{querySelector:s=>ids[s],createElement:t=>new El(t)},Option:class extends El{constructor(s,v){super('option');this.textContent=s;this.value=v}},confirm:()=>approve,
fetch:async(url,opts={})=>{
  const body=opts.body?JSON.parse(opts.body):null;calls.push({url,body});if(url==='/api/admin/access')return {ok:true,json:async()=>({local_admin:false})};let data={session};
  if(url==='/api/personalization')Object.assign(data,{model_drafts:true,contacts:[{username:'friend-A',name:'测试对象'}],personas:[],global_config:{revision:0},legacy:{entries:[]},fields:{length:['未知','简短']},labels:{length:'长度'},jobs:[]});
  else if(url.startsWith('/api/personalization/contact?'))Object.assign(data,{config:setting(),effective:{name:'助手',source:'builtin'},audit:[],conversation:{paused:true,no_proactive:false},analysis_jobs:[]});
  else if(url.endsWith('/analysis/preview')){assert.equal(body.contact,'friend-A');assert(body.start<body.end);data=detail();}
  else if(url.includes('/analysis/detail?'))data=detail();
  else if(url.endsWith('/analysis/run')){assert.equal(body.confirm_model_call,true);batchStatus='done';}
  else if(url.endsWith('/analysis/review')){assert.equal(body.revision,revision);assert.equal(body.value,'分条短消息');revision++;itemStatus='accepted';Object.assign(data,{application_id:'apply1'});}
  else if(url.endsWith('/analysis/undo')){assert.equal(body.revision,revision);revision++;itemStatus='undone';}
  else throw Error('unexpected API '+url);
  return {ok:true,json:async()=>data};
}};
function findText(root,fragment){for(const child of root.children||[]){if(child.tag==='button'&&child.textContent.includes(fragment))return child;const nested=findText(child,fragment);if(nested)return nested;}}
vm.createContext(env);vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],env);
(async()=>{
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/admin/access');
  await ids['#unlock'].onclick();ids['#contacts'].value='friend-A';await ids['#contacts'].onchange();
  assert.equal(ids['#modelDrafts'].hidden,false);assert(ids['#conversationStatus'].textContent.includes('暂停主动跟进'));
  assert(!calls.some(c=>c.url.includes('/analysis/')));
  ids['#analysisStart'].value='2026-09-01';ids['#analysisEnd'].value='2026-09-09';ids['#analysisSegments'].value='6';ids['#analysisCalls'].value='2';ids['#analysisBudget'].value='60000';
  await ids['#analysisPreview'].onclick();assert.equal(calls.filter(c=>c.url.endsWith('/analysis/preview')).length,1);
  let button=findText(ids['#analysisView'],'批准本批');assert(button);await button.onclick();assert(!calls.some(c=>c.url.endsWith('/analysis/run')));
  approve=true;await button.onclick();assert.equal(calls.filter(c=>c.url.endsWith('/analysis/run')).length,1);
  button=findText(ids['#analysisView'],'接受为观察记录');assert(button);await button.onclick();
  assert.equal(revision,1);assert.equal(itemStatus,'accepted');assert.equal(setting().preferences.length,undefined);
  button=findText(ids['#analysisView'],'撤销本次应用');assert(button);await button.onclick();assert.equal(itemStatus,'undone');
  ids['#contacts'].value='';await ids['#contacts'].onchange();assert.equal(ids['#modelDrafts'].hidden,true);
  console.log('PASS: 9 offline draft UI flow checks: load/read no model, consent display, preview, confirmation cancellation, explicit run, observation review, revision, undo, contact reset.');
})().catch(e=>{console.error(e);process.exitCode=1});
