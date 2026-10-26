import {JSDOM,ROOT,FRONTEND,loadStrategyCatalog} from '../support.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const root=FRONTEND;
const dom=new JSDOM(await fs.readFile(root+'index.html','utf8'),{url:'http://127.0.0.1:8765/#funding',pretendToBeVisual:true});
const {window}=dom;for(const key of ['window','document','Option','Event','location','history'])Object.defineProperty(globalThis,key,{configurable:true,value:window[key]});
const errors=[];window.addEventListener('error',e=>errors.push(e.error||e.message));process.on('unhandledRejection',e=>errors.push(e));
window.HTMLCanvasElement.prototype.getContext=()=>null;globalThis.ResizeObserver=class{observe(){}disconnect(){}};
window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
const now=Date.parse('2026-10-25T09:00:00Z'),calls=[];
const catalog={exchanges:{bithumb:['BTC'],okx:['BTC']},tick_exchanges:{bithumb:['BTC']},funding_exchanges:{okx:['BTC']},candle_exchanges:{},defaults:{legs:[{id:'A',exchange:'bithumb',ticker:'BTC'}],start:new Date(now-3600000).toISOString(),end:new Date(now).toISOString()}};
const query={data_type:'tick',exchange:'bithumb',ticker:'BTC',start:'2026-10-25T08:00:00Z',end:'2026-10-25T09:00:00Z',interval:'1s'};
const api=async(path,options={})=>{
 calls.push({path,...options});
 if(path.startsWith('/api/bounds?'))return {start:new Date(now-86400000).toISOString(),end:new Date(now-1).toISOString()};
 if(path.startsWith('/api/data/files/info?'))return {id:'tick-file',name:'08.parquet',exchange:'bithumb',data_kind:'tick',ticker:'BTC',format:'parquet',rows:1,bytes:10,query};
 if(path.startsWith('/api/data/files?'))return {items:[{id:'tick-file',kind:'file',name:'08.parquet',data_kind:'tick',exchange:'bithumb',ticker:'BTC'}],has_more:false,total:1,next_offset:null};
 if(path==='/api/data/ticks')return {config:options.body,times:[now-1000],values:[100],legs:[{id:'A',data_type:'tick',exchange:'bithumb',ticker:'BTC',currency:'KRW',quantity_unit:'BTC',values:[100],source_times:[now-999],trade_count:[1],volume:[1],buy_volume:[1],sell_volume:[0]}],stats:{min:100,max:100,latest_price:100,total_volume:1,buy_volume:1,sell_volume:0},meta:{rows_read:1,aligned_points:1,shown_points:1,direction_available:true,table_offset:0,table_limit:100,table_truncated:false,warnings:[]},rows:[{source_time:now-999,server_time:now-1000,side:'buy',price:100,quantity:1,qty:1}]};
 throw new Error('Unexpected '+path);
};
const $=id=>document.getElementById(id),pause=()=>new Promise(resolve=>setTimeout(resolve,5));
async function until(fn,label){for(let i=0;i<200;i++){if(fn())return;await pause();}throw new Error('Timeout '+label+' / '+$('data-error').textContent);}
const {initWorkspace}=await import('file://'+root+'shell/workspace.js');let stopped=0;await initWorkspace({api,getCatalog:()=>catalog,stopLive:async()=>{stopped++;}});await until(()=>document.querySelector('[data-node-id="tick-file"]'),'file tree ready');
assert.equal($('data-type').value,'funding');assert.equal($('module-data').hidden,false);assert.equal($('tab-data').getAttribute('aria-selected'),'true');assert.equal(location.hash,'#data');assert.equal($('data-exchange').value,'okx');assert.equal(stopped,1);assert.equal(document.querySelectorAll('[role="tab"]').length,9);assert.equal(document.querySelectorAll('[role="tabpanel"]').length,9);
$('tab-analysis').click();assert.equal($('module-analysis').hidden,false);assert.equal(location.hash,'#analysis');document.querySelector('[data-node-id="tick-file"]').click();await until(()=>calls.some(c=>c.path==='/api/data/ticks')&&!$('data-run').disabled,'sidebar tick auto-query');assert.equal($('module-data').hidden,false);assert.equal($('data-type').value,'tick');assert.equal($('data-exchange').value,'bithumb');assert.equal($('data-ticker').value,'BTC');assert.equal($('data-start').value,'2026-10-25T17:00');assert.equal($('data-end').value,'2026-10-25T18:00');assert.equal($('data-mid').textContent,'100');assert.equal($('data-error').hidden,true);assert.equal(location.hash,'#data');assert.deepEqual(errors,[]);
console.log('PASS full workspace legacy #funding redirects to unified funding data, nine tabs, analysis→sidebar tick auto-select/query with exact range, runtime errors=[]');

assert.equal($('manual-key-save').disabled,true);assert.match($('manual-error').textContent,/서버를 재시작/);dom.window.close();process.exit(0);
