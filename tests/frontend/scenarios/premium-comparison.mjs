import {JSDOM,ROOT,FRONTEND,loadStrategyCatalog} from '../support.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const root=FRONTEND;
const {window}=new JSDOM(await fs.readFile(root+'index.html','utf8'),{url:'http://localhost/',pretendToBeVisual:true});
for(const name of ['window','document','Event'])globalThis[name]=window[name];
globalThis.ResizeObserver=class{observe(){}disconnect(){}};
window.HTMLCanvasElement.prototype.getContext=()=>null;
const {PremiumComparison,initPremiums}=await import(root+'features/analysis/premiums.js');
const $=id=>document.getElementById('premium-compare-'+id),settle=()=>new Promise(resolve=>setTimeout(resolve,10));
const H=3600000,end=Date.parse('2026-10-27T00:00:00Z');
const tickers=Array.from({length:12},(_,i)=>'COIN'+i);
const payload=ticker=>({ticker,end_ms:end,items:[{hour_ms:end-H,mean_pct:Number(ticker.slice(4)),samples:3600,status:'complete'}]});
let active=0,peak=0,deferred=false;
const queue=[];
const api=async path=>{
 const ticker=new URL(path,'http://localhost').searchParams.get('ticker');
 active++;peak=Math.max(peak,active);
 try{
  if(deferred)await new Promise(resolve=>queue.push(resolve));
  if(ticker==='COIN2')throw new Error('one symbol failed');
  return payload(ticker);
 }finally{active--;}
};
const comparison=new PremiumComparison({api});
try{
 comparison.setUniverse(tickers.map(ticker=>({ticker})),end);
 assert.equal(comparison.selected.size,3);await comparison.refresh();
 assert.equal(comparison.result.series.length,3);
 assert.deepEqual(comparison.result.series.map(line=>line.values.at(-1)),[0,1,null]);
 assert.match($('status').textContent,/조회 실패: COIN2/);
 $('details').open=true;await settle();assert.equal($('rows').rows.length,168);
 assert.equal($('head').rows[0].cells.length,4);
 for(let i=3;i<10;i++)assert.equal(comparison.toggle('COIN'+i,true),true);
 assert.equal(comparison.toggle('COIN10',true),false);
 assert.equal(comparison.selected.size,10);
 await comparison.refresh();assert.equal(comparison.result.series.length,10);assert.ok(peak<=3);
 $('days').value='3';$('days').dispatchEvent(new Event('change'));await comparison.refresh();
 assert.equal(comparison.result.times.length,72);assert.equal($('rows').rows.length,72);
 // New selections supersede old pending batches, without exceeding the request limit.
 deferred=true;const old=comparison.refresh();await settle();assert.equal(queue.length,3);
 comparison.toggle('COIN0',false);const latest=comparison.refresh();
 for(let i=0;i<6;i++){const pending=queue.splice(0);for(const resolve of pending)resolve();await settle();}
 await old;await latest;
 assert.equal(comparison.result.series.length,9);assert.ok(comparison.result.series.every(line=>line.ticker!=='COIN0'));assert.ok(peak<=3);
 // Closing cancels pending work and invalidates in-flight results.
 const before=comparison.refresh();await settle();comparison.cancel();
 for(const resolve of queue.splice(0))resolve();await before;assert.equal(comparison.result,null);
 deferred=false;await comparison.refresh();
 $('clear').click();assert.equal(comparison.selected.size,0);assert.equal(comparison.result,null);assert.equal($('csv').disabled,true);
 comparison.setUniverse(tickers.map(ticker=>({ticker})),end);assert.equal(comparison.selected.size,0,'manual clear survives refresh');
 console.log('Comparison DOM: default/10-symbol cap, 3/7 days, partial failure, aligned table, max 3 requests, stale-selection response rejection, cancellation and manual clear passed.');
}finally{comparison.cancel();window.close();}
