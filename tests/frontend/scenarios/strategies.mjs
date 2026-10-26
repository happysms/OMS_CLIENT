import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM,ROOT,FRONTEND,loadStrategyCatalog} from '../support.mjs';
const root=ROOT;
const dom=new JSDOM(await readFile(root+'/frontend/index.html','utf8'),{url:'http://localhost/#strategies'});
for(const name of ['window','document','Option','Event','CustomEvent','HTMLElement'])globalThis[name]=dom.window[name];
const {initStrategies}=await import(root+'/frontend/features/strategies/controller.js');
const {initReplay}=await import(root+'/frontend/features/replay/controller.js');
const catalog=await loadStrategyCatalog();
const calls=[];let next=1;
async function api(path,options={}){
 calls.push({path,...options});
 if(path==='/api/strategies'&&options.method!=='POST')return structuredClone(catalog);
 if(path==='/api/strategies'&&options.method==='POST'){
  const body=options.body,template=catalog.templates.find(t=>t.type===body.type),item={...body,id:'user-'+next++,revision:1,archived:false,executable:template.executable,execution_note:template.execution_note,required_venues:template.required_venues};catalog.items.push(item);return structuredClone(item);
 }
 if(path.endsWith('/revisions')){
  const id=path.split('/')[3],index=catalog.items.findIndex(i=>i.id===id),previous=catalog.items[index];
  assert.equal(options.body.expected_revision,previous.revision);
  catalog.items[index]={...previous,...options.body,revision:previous.revision+1};return structuredClone(catalog.items[index]);
 }
 if(path.endsWith('/archive')){const item=catalog.items.find(i=>i.id===path.split('/')[3]);item.archived=options.body.archived;return structuredClone(item);}
 if(path==='/api/replays')return {items:[],next_before:null};
 throw new Error('Unexpected '+path);
}
const manager=initStrategies({api}),replay=initReplay({api,getCatalog:()=>({exchanges:{bithumb:['BTC']}})});
const $=id=>document.getElementById(id),flush=()=>new Promise(resolve=>setTimeout(resolve,10));
try{
 manager.onEnter();await flush();
 assert.equal($('strategies-list').rows.length,3);
 assert.match($('strategies-list').textContent,/실행 연결 필요/);
 assert.equal($('strategy-param-theory_mode').value,'midprice');
 $('strategies-name').value='내 매수 전용';$('strategy-param-direction').value='buy';
 $('strategies-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
 assert.equal(catalog.items.length,4);assert.equal(catalog.items[3].parameters.direction,'buy');
 assert.equal($('strategies-save').textContent,'새 버전 저장');
 $('strategy-param-direction').value='sell';$('strategies-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
 assert.equal(catalog.items[3].revision,2);assert.equal(catalog.items[3].parameters.direction,'sell');
 assert.equal($('replay-strategy').options.length,4);
 const row=[...$('strategies-list').rows].find(r=>r.cells[0].textContent==='내 매수 전용');row.querySelectorAll('button')[1].click();await flush();
 assert.equal($('replay-strategy').value,'user-1@2');assert.equal($('replay-run').disabled,false);
 const point=[...$('strategies-list').rows].find(r=>r.cells[0].textContent===catalog.items.find(item=>item.id==='builtin_point2').name);point.querySelectorAll('button')[1].click();
 assert.equal($('replay-strategy').value,'builtin_point2@1');assert.equal($('replay-run').disabled,true);
 assert.match($('replay-strategy-help').textContent,/실행에는/);
 $('strategies-new').click();$('strategies-type').value='point2';$('strategies-type').dispatchEvent(new Event('change'));
 assert.equal($('strategy-param-baseline_bps').value,'-100');assert.equal($('strategy-param-hedge_execution').value,'post_only');
 $('strategies-name').value='Point 버퍼 25';$('strategy-param-buffer_bps').value='25';$('strategies-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
 assert.equal(catalog.items.at(-1).parameters.buffer_bps,'25');assert.equal(catalog.items.at(-1).executable,false);
 const savedPoint=[...$('strategies-list').rows].at(-1);savedPoint.querySelectorAll('button')[2].click();await flush();
 assert.equal(catalog.items.at(-1).archived,true);assert.match($('strategies-list').textContent,/보관됨/);
 assert.equal($('replay-theory'),null,'The account must not solicit a fixed theory price');
 console.log('Strategy UI checks passed: templates, custom registration, revision edits, maker execution selection, point execution gating, archive, strategy-provided theory setting.');
}finally{replay.onLeave();dom.window.close();}
