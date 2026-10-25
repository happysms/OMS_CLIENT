import {TimeChart} from '../../shared/charts.js';
import {$, exchanges, milliseconds, number, currency, price, time, wait, errorAt, clearError, tableRows, source, range} from '../../shared/dom.js';
import {createHistoryControls} from '../../shared/source-controls.js';
import {createJobRunner} from '../../shared/jobs.js';

export function initData({api,getCatalog}) {
  const state={versions:{data:0},data:null,dataJob:null};
  const csv={version:0,busy:false,result:null,controller:null,selection:null,downloading:false};
  const dataChart=new TimeChart($('data-chart'),{format:price,onHover:t=>dataChart.setCursor(t),onZoom:r=>dataChart.setZoom(r)});
  const job=createJobRunner({api,prefix:'data',getVersion:()=>state.versions.data});
  function changed(){
    state.versions.data++;
    $('data-run').disabled=false;$('data-run').textContent='데이터 조회';
    $('data-message').textContent='설정이 바뀌었습니다. 실행하면 새 조건을 적용합니다.';
    $('data-csv').disabled=true;state.dataJob=null;
    invalidateCsv();
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
  function csvSelection() {
    return {...source('data'),...range('data'),mode:$('data-export-mode').value};
  }
  function invalidateCsv(message='선택 조건이 바뀌었습니다. CSV를 다시 생성하세요.') {
    csv.version++;csv.controller?.abort();csv.controller=null;csv.busy=false;csv.downloading=false;csv.result=null;csv.selection=null;
    $('data-export-create').disabled=false;$('data-export-create').textContent='CSV 생성';
    $('data-export-download').disabled=true;$('data-export-download').textContent='↓ 파일 다운로드';
    $('data-export-result').hidden=true;$('data-export-message').textContent=message;clearError('data-export');
  }
  function currentCsv(version,selection) {
    if(version!==csv.version)return false;
    let current;try{current=JSON.stringify(csvSelection());}catch{current=null;}
    if(current!==selection){invalidateCsv();return false;}
    return true;
  }
  function csvDescription() {
    $('data-export-help').textContent=$('data-export-mode').value==='raw'
      ?'원본 호가의 모든 컬럼과 행을 시간순으로 내보냅니다. desktopTime은 원본 밀리초 타임스탬프를 유지합니다.'
      :'선택한 평균 간격의 전체 구간을 내보냅니다. 한국 시간, 거래소·종목·통화, 중간가·매수/매도 호가·스프레드·원본 행 수를 포함합니다.';
  }
  async function createCsv() {
    if(csv.busy||!$('data-form').reportValidity())return;
    clearError('data-export');
    const version=++csv.version;let selection;
    try{
      const config=csvSelection();selection=JSON.stringify(config);csv.selection=selection;csv.result=null;csv.busy=true;csv.downloading=false;
      csv.controller?.abort();csv.controller=new AbortController();const signal=csv.controller.signal;
      $('data-export-create').disabled=true;$('data-export-create').textContent='CSV 생성 중…';$('data-export-download').disabled=true;$('data-export-download').textContent='↓ 파일 다운로드';$('data-export-result').hidden=true;
      $('data-export-message').textContent='서버에 CSV 생성을 요청하고 있습니다.';
      const created=await api('/api/data/exports',{method:'POST',body:config,signal}),began=Date.now();
      while(currentCsv(version,selection)){
        const result=await api('/api/data/exports/'+encodeURIComponent(created.id),{signal});
        if(!currentCsv(version,selection))return;
        if(result.status==='complete'){
          if(result.download_available===false)throw new Error('파일 보관 기간이 지났습니다. CSV를 다시 생성하세요.');
          csv.result={...result,id:created.id};
          $('data-export-filename').textContent=result.filename||'oms-data.csv';$('data-export-rows').textContent=number(result.rows,0)+'행';
          $('data-export-columns').textContent=number(Array.isArray(result.columns)?result.columns.length:result.columns,0)+'개';
          $('data-export-size').textContent=Number.isFinite(result.bytes)?(result.bytes<1024?number(result.bytes,0)+' B':result.bytes<1048576?number(result.bytes/1024,1)+' KiB':number(result.bytes/1048576,1)+' MiB'):'—';
          $('data-export-expiry').textContent=(result.expires_at?`${time(result.expires_at)} KST까지 보관합니다. `:'')+'서버 재시작이나 보관 한도 초과 시 삭제될 수 있으니 파일을 다운로드하세요.';
          $('data-export-result').hidden=false;$('data-export-download').disabled=false;
          $('data-export-message').textContent=`${config.mode==='raw'?'원본 행':'평균 집계'} CSV를 생성했습니다. 파일 다운로드를 누르세요.`;
          return;
        }
        if(['failed','cancelled','stopped'].includes(result.status))throw new Error(typeof result.error==='string'?result.error:'CSV를 생성하지 못했습니다. 기간을 확인하고 다시 시도하세요.');
        $('data-export-message').textContent=`${result.status==='queued'?'CSV 생성 대기 중':'서버에서 CSV 생성 중'} · ${Math.floor((Date.now()-began)/1000)}초`;
        if(Date.now()-began>600000)throw new Error('CSV 생성 대기가 길어지고 있습니다. 기간을 줄여 다시 시도하세요.');
        await wait(500);
      }
    }catch(error){if(version===csv.version&&error.name!=='AbortError'){errorAt('data-export',error);$('data-export-message').textContent='CSV를 생성하지 못했습니다.';}}
    finally{if(version===csv.version){csv.busy=false;$('data-export-create').disabled=false;$('data-export-create').textContent='CSV 생성';}}
  }
  async function downloadCsv() {
    if(!csv.result||csv.downloading)return;
    const version=csv.version,selection=csv.selection,result=csv.result;
    if(!currentCsv(version,selection))return;
    clearError('data-export');csv.downloading=true;csv.controller?.abort();csv.controller=new AbortController();
    $('data-export-download').disabled=true;$('data-export-download').textContent='다운로드 중…';
    try{
      const response=await fetch('/api/data/exports/'+encodeURIComponent(result.id)+'/csv',{credentials:'same-origin',headers:{Accept:'text/csv'},signal:csv.controller.signal});
      if(!currentCsv(version,selection))return;
      if(response.status===401){location.assign('/login?next=%2F');throw new Error('로그인이 만료되었습니다. 다시 로그인해주세요.');}
      if(!response.ok){
        let details={};try{details=await response.json();}catch{}
        if(!currentCsv(version,selection))return;
        if([404,410].includes(response.status)){
          csv.result=null;$('data-export-result').hidden=true;$('data-export-message').textContent='파일 보관 기간이 지났습니다. CSV를 다시 생성하세요.';
        }
        throw new Error(typeof details.error==='string'?details.error:'CSV를 다운로드하지 못했습니다. 다시 시도하세요.');
      }
      const blob=await response.blob();if(!currentCsv(version,selection))return;
      const disposition=response.headers.get('Content-Disposition')||'',extended=disposition.match(/filename\*=UTF-8''([^;]+)/i),plain=disposition.match(/filename="([^"]+)"|filename=([^;]+)/i);
      let filename=result.filename||'oms-data.csv';
      if(extended){try{filename=decodeURIComponent(extended[1]);}catch{}}
      else if(plain)filename=(plain[1]||plain[2]).trim();
      filename=filename.replace(/[\\/\x00-\x1f]/g,'_');
      const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      $('data-export-message').textContent='CSV 파일 다운로드를 시작했습니다.';
    }catch(error){if(version===csv.version&&error.name!=='AbortError')errorAt('data-export',error);}
    finally{if(version===csv.version){csv.downloading=false;$('data-export-download').disabled=!csv.result;$('data-export-download').textContent='↓ 파일 다운로드';}}
  }
  $('data-form').addEventListener('submit',runData);
  $('data-csv').addEventListener('click',exportData);
  $('data-export-create').addEventListener('click',createCsv);
  $('data-export-download').addEventListener('click',downloadCsv);
  $('data-export-mode').addEventListener('change',()=>{invalidateCsv();csvDescription();});
  for(const input of $('data-form').querySelectorAll('input'))input.addEventListener('input',()=>invalidateCsv());
  const controls=createHistoryControls({prefix:'data',api,getCatalog,onChanged:changed});
  return {ready:controls.ready,refresh:controls.refresh,onEnter(){dataChart.draw();}};
}
