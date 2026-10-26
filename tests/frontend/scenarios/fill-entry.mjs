import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM,ROOT,FRONTEND,loadStrategyCatalog} from '../support.mjs';
const root=ROOT;
const dom=new JSDOM(await readFile(root+'/frontend/index.html','utf8'),{url:'http://localhost/#replay'});
for(const name of ['window','document','Option','Event','HTMLElement'])globalThis[name]=dom.window[name];
const {bookView,initReplay,FillInspector}=await import(root+'/frontend/features/replay/controller.js');
const opened=[];FillInspector.prototype.open=async function(...args){opened.push(args);};
const start=1792983600000;
const book={time:start,bids:[100,99,98,97,96].map(price=>({price,quantity:2})),asks:[102,103,104,105,106].map(price=>({price,quantity:3}))};
const order=(id,side,price,remaining,status='live')=>({id,side,price,remaining,status,owner:'manual',queue_known:price>=96&&price<=106,queue_ahead:2});
const orders=[order('a','buy',100,1),order('b','buy',100,2,'cancel_pending'),order('c','buy',90,4),order('d','sell',110,5,'in_flight'),order('e','buy',99.5,6),order('f','sell',103,7)];
let snapshot={id:'run1',revision:1,status:'running',config:{ticker:'XYZ',start_ms:start,end_ms:start+60000,venues:['bithumb','okx'],auto_mm:true},virtual_time_ms:start,speed:1,fast:false,paused:false,total_events:500,skipped_events:0,event_count:100,fill_count:1,books:{bithumb:book,okx:book},orders,order_events:[],ticks:[{time:start,venue:'bithumb',price:100,qty:-1,direction_known:true}],account:{cash:1000,free_cash:100,coin:10,free_coin:5,equity:2000,realized_pnl:3,unrealized_pnl:4,theory_unrealized_pnl:5,total_pnl:7},fills:[{sequence:1,timestamp:start,order_id:'a',side:'buy',price:100,quantity:1,fee:.1,realized_pnl:0,account:{realized_pnl:0,unrealized_pnl:-.1,theory_unrealized_pnl:2,total_pnl:-.1,equity:999.9}}]};
const strategyCatalog=await loadStrategyCatalog();
const calls=[];
async function api(path,options={}){
 calls.push({path,...options});
 if(path==='/api/strategies')return structuredClone(strategyCatalog);
 if(path==='/api/strategies/builtin_single_maker?revision=1')return structuredClone(strategyCatalog.items[0]);
 if(path==='/api/replays')return {items:[{...snapshot}]};
 if(path.endsWith('/control')){
  const p=options.body;snapshot={...snapshot,revision:snapshot.revision+1};
  if(p.action==='pace')Object.assign(snapshot,{speed:p.speed,fast:p.fast});
  if(p.action==='pause')Object.assign(snapshot,{status:'paused',paused:true});
  if(p.action==='play')Object.assign(snapshot,{status:'running',paused:false});
  return structuredClone(snapshot);
 }
 if(path==='/api/replays/run1/result.json')return new Blob([JSON.stringify(snapshot)],{type:'application/json'});
 if(path==='/api/replays/run1')return structuredClone(snapshot);
 throw new Error('Unexpected '+path);
}
const controller=initReplay({api,getCatalog:()=>({exchanges:{bithumb:['XYZ','BTC']}})});
const $=id=>document.getElementById('replay-'+id);
const flush=()=>new Promise(resolve=>setTimeout(resolve,15));
try{
 controller.onEnter();await flush();
 $('fills').querySelector('[data-fill-context]').click();assert.equal(opened[0][0],'run1');assert.equal(opened[0][1].sequence,1);
 assert.equal($('outside').hidden,false);
 assert.equal($('outside-rows').children.length,2);
 assert.match($('outside-rows').textContent,/90/);assert.match($('outside-rows').textContent,/110/);
 assert.ok($('outside').compareDocumentPosition($('book'))&dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal($('book').querySelectorAll('[data-market="true"]').length,10);
 assert.equal($('book').querySelectorAll('[data-market="false"]').length,1);
 const bid=[...$('book').rows].find(r=>r.cells[1].textContent==='100');
 assert.equal(bid.cells[2].textContent,'2');assert.match(bid.cells[3].textContent,/1 .* \+ 2/);
 assert.equal($('orders').children.length,6);assert.equal($('okx-panel').hidden,false);
 assert.match($('ticks').textContent,/매도 체결/);assert.match($('fills').textContent,/-0.1/);
 $('speed').value='32';$('speed').dispatchEvent(new Event('change'));await flush();
 assert.equal(calls.at(-1).body.speed,32);assert.equal($('speed-label').textContent,'32×');
 $('fast').checked=true;$('fast').dispatchEvent(new Event('change'));await flush();
 assert.equal(calls.at(-1).body.fast,true);
 $('pause').click();await flush();assert.equal($('pause').textContent,'재개');
 $('pause').click();await flush();assert.equal($('pause').textContent,'일시정지');
 $('orders').querySelector('[data-cancel="c"]').click();await flush();assert.deepEqual(calls.at(-1).body,{action:'cancel',order_id:'c'});
 assert.equal(bookView(null,orders).outside.length,orders.length);
 assert.equal(bookView(book,orders).levels.filter(l=>l.market).length,10);
 snapshot={...snapshot,revision:snapshot.revision+1,status:'finished',virtual_account:{id:'va_run1',name:'나의 MM 실험',initial_krw:'7777',initial_coin:'2',ticker:'XYZ'},initial_config:{strategy:structuredClone(strategyCatalog.items[0]),ticker:'XYZ',start_ms:start,end_ms:start+60000,initial_krw:'7777',initial_coin:'2',fee_bps:'3',quantity_step:'0.01',min_notional:'5',latency_ms:40,theory_price:null,venues:['bithumb','okx'],auto_mm:false,fast:true,speed:16}};
 controller.onLeave();controller.onEnter();await flush();
 assert.match($('account-info').textContent,/나의 MM 실험/);assert.match($('account-info').textContent,/완료 결과 저장됨/);
 assert.match($('history').textContent,/나의 MM 실험/);assert.match($('history').textContent,/7,777원/);
 assert.equal($('copy').disabled,false);const requestCount=calls.length;
 $('copy').click();assert.equal($('cash').value,'7777');assert.equal($('coin').value,'2');assert.equal($('account-name').value,'나의 MM 실험 복사');
 assert.equal($('speed').value,'16');assert.equal($('mm').checked,false);assert.equal($('latency').value,'40');
 assert.equal(calls.length,requestCount,'Preparing a new account must not start a trade or change an old account');
 let download='';dom.window.HTMLAnchorElement.prototype.click=function(){download=this.download;};
 $('result').click();await flush();assert.equal(calls.at(-1).path,'/api/replays/run1/result.json');assert.equal(download,'replay-run1-result.json');
 console.log('Account DOM checks passed: named account summary/history, independent initial-condition copy, full result download.');
 console.log('Replay DOM checks passed: outside orders above all 5 levels, in-range overlays, separate quantities, ticks, PnL, 32x, fast, pause/resume, cancel.');
}finally{controller.onLeave();dom.window.close();}
