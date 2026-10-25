import {TimeChart} from '../../shared/charts.js';
import {$, exchanges, milliseconds, number, currency, price, time, errorAt, clearError, tableRows, source, range} from '../../shared/dom.js';
import {createHistoryControls} from '../../shared/source-controls.js';
import {createJobRunner} from '../../shared/jobs.js';

export function initData({api,getCatalog}) {
  const state={versions:{data:0},data:null,dataJob:null};
  const dataChart=new TimeChart($('data-chart'),{format:price,onHover:t=>dataChart.setCursor(t),onZoom:r=>dataChart.setZoom(r)});
  const job=createJobRunner({api,prefix:'data',getVersion:()=>state.versions.data});
  function changed(){
    state.versions.data++;
    $('data-run').disabled=false;$('data-run').textContent='데이터 조회';
    $('data-message').textContent='설정이 바뀌었습니다. 실행하면 새 조건을 적용합니다.';
    $('data-csv').disabled=true;state.dataJob=null;
  }
  async function runData(event) {
    event.preventDefault();const version=++state.versions.data;clearError('data');$('data-run').disabled=true;$('data-run').textContent='조회 중…';$('data-csv').disabled=true;
    try{
      const selection=source('data'),config={...range('data'),legs:[{id:'A',...selection}],formula:'A',unit:currency(selection.exchange),ewm_span:1,max_points:6000};
      const response=await job('/api/analyses',config,version);if(!response)return;
      const r=response.result,leg=r.legs?.[0];if(!leg)throw new Error('조회 결과에 호가 소스가 없습니다.');
      state.data=r;state.dataJob=response.id;const i=r.times.length-1;
      $('data-mid').textContent=price(leg.mid[i]);$('data-currency').textContent=leg.currency||config.unit;
      $('data-bidask').textContent=price(leg.bid[i])+' / '+price(leg.ask[i]);
      $('data-spread').textContent=Number.isFinite(leg.ask[i])&&Number.isFinite(leg.bid[i])?price(leg.ask[i]-leg.bid[i]):'—';
      $('data-rows').textContent=number(r.meta.rows_read,0);$('data-points').textContent=`${number(r.meta.aligned_points,0)}개 집계 구간 · 표시 ${number(r.times.length,0)}개`;
      $('data-chart-label').textContent=`${exchanges[selection.exchange]||selection.exchange} ${selection.ticker} · ${leg.currency||config.unit} · ${time(r.times[0])} ~ ${time(r.times.at(-1))} KST`;
      dataChart.setZoom(null);dataChart.setData(r.times,[{label:'중간가',values:leg.mid,color:'#5bdfb7',width:1.8},{label:'매수 1호가',values:leg.bid,color:'#78b4e8'},{label:'매도 1호가',values:leg.ask,color:'#f1b66e'}],{gapMs:milliseconds[config.interval]*Math.max(1,Math.ceil((r.meta.aligned_points||r.times.length)/Math.max(1,r.times.length)))});
      $('data-placeholder').hidden=leg.mid.some(Number.isFinite);$('data-csv').disabled=!r.times.length;
      const rows=[];for(let i=r.times.length-1;i>=Math.max(0,r.times.length-100);i--)rows.push([time(r.times[i]),price(leg.mid[i]),price(leg.bid[i]),price(leg.ask[i]),Number.isFinite(leg.ask[i])&&Number.isFinite(leg.bid[i])?price(leg.ask[i]-leg.bid[i]):'—']);
      tableRows('data-table',rows,5,'선택한 구간에 조회 가능한 호가가 없습니다.');
      $('data-message').textContent='서버 조회 완료'+((r.meta.warnings||[]).length?' · '+r.meta.warnings.join(' '):' · 차트와 표는 평균 간격으로 집계한 표본입니다.');
    }catch(error){if(version===state.versions.data){errorAt('data',error);$('data-message').textContent='데이터를 조회하지 못했습니다.';}}
    finally{if(version===state.versions.data){$('data-run').disabled=false;$('data-run').textContent='데이터 조회';}}
  }
  async function exportData() {
    if(!state.dataJob)return;const id=state.dataJob;clearError('data');$('data-csv').disabled=true;
    try{
      const response=await fetch('/api/analyses/'+encodeURIComponent(id)+'/csv',{credentials:'same-origin',headers:{Accept:'text/csv'}});
      if(response.status===401){location.assign('/login?next=%2F');throw new Error('로그인이 만료되었습니다. 다시 로그인해주세요.');}
      if(!response.ok){let details={};try{details=await response.json();}catch{}throw new Error(typeof details.error==='string'?details.error:'CSV를 내보내지 못했습니다. 다시 조회해주세요.');}
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download='oms-data.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){errorAt('data',error);}finally{$('data-csv').disabled=!state.dataJob||!state.data?.times.length;}
  }
  $('data-form').addEventListener('submit',runData);
  $('data-csv').addEventListener('click',exportData);
  const controls=createHistoryControls({prefix:'data',api,getCatalog,onChanged:changed});
  return {ready:controls.ready,refresh:controls.refresh,onEnter(){dataChart.draw();}};
}
