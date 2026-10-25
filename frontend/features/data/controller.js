import {TimeChart} from '../../shared/charts.js';
import {$, exchanges, milliseconds, number, currency, price, time, wait, errorAt, clearError, tableRows, source, range} from '../../shared/dom.js';
import {createHistoryControls} from '../../shared/source-controls.js';
import {createJobRunner} from '../../shared/jobs.js';

export function initData({api,getCatalog}) {
  const state={versions:{data:0},data:null,dataJob:null,selection:null};
  const csv={version:0,busy:false,result:null,controller:null,selection:null,downloading:false};
  const intervalLabels={'1s':'1초','10s':'10초','1min':'1분','5min':'5분','1h':'1시간'};
  const originalQuote=value=>Number.isFinite(value)?new Intl.NumberFormat('en-US',{maximumFractionDigits:20}).format(value):'—';
  const dataChart=new TimeChart($('data-chart'),{format:price,onHover:t=>dataChart.setCursor(t),onZoom:r=>dataChart.setZoom(r)});
  const job=createJobRunner({api,prefix:'data',getVersion:()=>state.versions.data});
  function sizeColumns(show,unit='USDT',last=true){
    $('data-sizes').hidden=!show;
    for(const side of ['bid','ask']){
      const header=$('data-table-'+side+'-size-label');header.hidden=!show;
      header.textContent=`${last?'':'평균 '}${side==='bid'?'매수':'매도'} 수량 (${unit})`;
    }
    $('data-source-help').hidden=!show;
  }
  function describePriceMode(){
    const last=$('data-price-mode').value==='last';
    $('data-mid-label').textContent=last?'마지막 구간 중간가':'마지막 구간 평균 중간가';
    $('data-bidask-label').textContent=last?'구간 마지막 매수 / 매도 1호가':'구간 평균 매수 / 매도 호가';
    $('data-spread-label').textContent=last?'구간 마지막 스프레드':'평균 스프레드';
    $('data-spread-help').textContent=last?'같은 원본 시각의 매도 1호가 − 매수 1호가':'평균 매도 호가 − 평균 매수 호가';
    $('data-price-help').textContent=last
      ?'각 구간의 마지막 원본 행에서 매수·매도 1호가를 함께 가져옵니다. 중간가 = (매수 1호가 + 매도 1호가) ÷ 2이므로 소수점이 생길 수 있습니다.'
      :'구간 내 여러 호가를 평균 내므로 매수·매도 가격에도 소수점이 생길 수 있습니다. 중간가 = (평균 매수 호가 + 평균 매도 호가) ÷ 2입니다.';
    $('data-chart-title').textContent=last?'구간 마지막 중간가와 1호가':'구간 평균 중간가와 호가';
    $('data-chart-series').textContent=last?'원본 중간가 · 매수 1호가 · 매도 1호가':'평균 중간가 · 평균 매수 호가 · 평균 매도 호가';
    $('data-chart').setAttribute('aria-label',last?'저장된 구간 마지막 원본 중간가와 매수·매도 1호가 차트':'저장된 구간 평균 중간가와 매수·매도 호가 차트');
    $('data-table-title').textContent=last?'구간 마지막 원본 호가':'구간 평균 호가';
    $('data-table-help').textContent=last?'각 구간의 마지막 원본 호가 · 차트 표본 중 최근 100개 · 전체 결과는 조회 결과 CSV':'선택한 간격의 평균 · 차트 표본 중 최근 100개 · 전체 집계는 조회 결과 CSV';
    $('data-table-time-label').textContent=last?'원본 시각 (KST)':'구간 시작 (KST)';
    $('data-table-mid-label').textContent=last?'중간가':'평균 중간가';
    $('data-table-bid-label').textContent=last?'매수 1호가':'평균 매수 호가';
    $('data-table-ask-label').textContent=last?'매도 1호가':'평균 매도 호가';
    $('data-table-spread-label').textContent=last?'스프레드':'평균 스프레드';
  }
  function selectionKey(){
    return JSON.stringify({...source('data'),start:$('data-start').value,end:$('data-end').value,interval:$('data-interval').value,price_mode:$('data-price-mode').value});
  }
  function clearData(message='종목과 기간을 선택한 뒤 데이터 조회를 누르세요.'){
    state.data=null;state.dataJob=null;
    for(const id of ['data-mid','data-spread','data-rows'])$(id).textContent='—';
    $('data-currency').textContent='조회 후 종목과 통화를 표시합니다.';
    $('data-bidask').textContent='— / —';$('data-bucket').textContent='조회 후 호가 기준 시각과 조회 방식을 표시합니다.';
    $('data-sizes').textContent='';$('data-source-help').textContent='';sizeColumns(false);
    $('data-points').textContent='선택한 구간을 조회하세요.';
    $('data-chart-label').textContent='조회한 종목·기간·간격·조회 방식을 표시합니다.';
    describePriceMode();
    dataChart.setData([],[]);dataChart.setZoom(null);dataChart.setCursor(null);
    $('data-placeholder').hidden=false;$('data-placeholder').querySelector('strong').textContent=message;
    tableRows('data-table',[],5,'조회 결과가 없습니다. 조건을 확인한 뒤 데이터 조회를 누르세요.');
    $('data-csv').disabled=true;
  }
  function changed(){
    const selection=selectionKey();if(selection===state.selection)return;
    state.selection=selection;
    state.versions.data++;
    $('data-run').disabled=false;$('data-run').textContent='데이터 조회';
    $('data-message').textContent='설정이 바뀌었습니다. 데이터 조회를 누르면 새 조건으로 계산합니다.';
    clearError('data');clearData();
    let sameCsv=false;try{sameCsv=csv.selection===JSON.stringify(csvSelection());}catch{}
    if(!sameCsv)invalidateCsv();
  }
  function currentQuery(version,selection){
    if(version!==state.versions.data)return false;
    if(selection!==selectionKey()){changed();return false;}
    return true;
  }
  async function runData(event) {
    event.preventDefault();const version=++state.versions.data,selected=selectionKey();state.selection=selected;
    clearError('data');clearData('서버에서 선택한 구간의 호가를 조회하고 있습니다.');$('data-run').disabled=true;$('data-run').textContent='조회 중…';
    try{
      const selection=source('data'),config={...range('data'),price_mode:$('data-price-mode').value,legs:[{id:'A',...selection}],formula:'A',unit:currency(selection.exchange),ewm_span:1,max_points:6000};
      const response=await job('/api/analyses',config,version);if(!response||!currentQuery(version,selected))return;
      const r=response.result,leg=r.legs?.[0];if(!leg)throw new Error('조회 결과에 호가 소스가 없습니다.');
      if(leg.exchange!==selection.exchange||String(leg.ticker).toUpperCase()!==selection.ticker.toUpperCase())throw new Error('요청한 종목과 조회 결과가 다릅니다. 다시 조회해주세요.');
      if(r.meta?.price_mode!==config.price_mode)throw new Error('요청한 호가 조회 방식과 결과가 다릅니다. 다시 조회해주세요.');
      state.data=r;state.dataJob=response.id;const i=r.times.length-1;
      const last=config.price_mode==='last',hasQuoteTimes=last&&Array.isArray(leg.quote_times)&&leg.quote_times.length===r.times.length&&leg.quote_times.every(Number.isFinite);
      const hasSizes=leg.source_kind==='orderbook_1s'&&leg.quantity_unit==='USDT'&&['bid_size','ask_size'].every(key=>Array.isArray(leg[key])&&leg[key].length===r.times.length&&leg[key].every(value=>Number.isFinite(value)&&value>=0));
      sizeColumns(hasSizes,leg.quantity_unit,last);
      const displayTimes=hasQuoteTimes?leg.quote_times:r.times;
      const identity=`${exchanges[leg.exchange]||leg.exchange} · ${leg.ticker} · ${leg.currency||config.unit}`,interval=intervalLabels[config.interval]||config.interval;
      const description=(hasSizes?'1초 수집 · ':'')+(last?`${interval} 간격 · 마지막 원본 호가`:`${interval} 평균`);
      const basis=hasQuoteTimes?`마지막 ${hasSizes?'채택':'원본'} 시각 ${time(displayTimes[i])} KST`:`마지막 구간 시작 ${time(r.times[i])} KST`;
      const quotePrice=last?originalQuote:price;
      $('data-mid').textContent=price(leg.mid[i]);$('data-currency').textContent=identity;
      $('data-bidask').textContent=quotePrice(leg.bid[i])+' / '+quotePrice(leg.ask[i]);
      if(hasSizes){
        $('data-sizes').textContent=`${last?'':'평균 '}매수 / 매도 수량 ${originalQuote(leg.bid_size[i])} / ${originalQuote(leg.ask_size[i])} ${leg.quantity_unit}`;
        $('data-source-help').textContent='가격 단위 KRW · 수량은 실제 USDT입니다. 1초마다 최신 호가를 채택하므로 거래소 갱신 사이에는 같은 값이 반복될 수 있습니다. '+(last?'표시 시각은 실제 채택 시점이며 가격과 수량은 같은 원본 행입니다.':'표시 시각은 구간 시작이며 가격과 수량 모두 같은 구간의 유효 원본 행들의 평균입니다.')+' 원본 CSV에 1초 기준·실제 채택·거래소·수신 시각과 호가 나이를 함께 저장합니다.';
      }
      $('data-bucket').textContent=`${basis} · ${description}`;
      $('data-spread').textContent=Number.isFinite(leg.ask[i])&&Number.isFinite(leg.bid[i])?price(leg.ask[i]-leg.bid[i]):'—';
      $('data-rows').textContent=number(r.meta.rows_read,0);$('data-points').textContent=`${number(r.meta.aligned_points,0)}개 집계 구간 · 표시 ${number(r.times.length,0)}개`;
      $('data-chart-label').textContent=`${identity} · ${time(displayTimes[0])} ~ ${time(displayTimes.at(-1))} KST · ${description}`;
      dataChart.setZoom(null);dataChart.setData(displayTimes,[{label:last?'중간가':'평균 중간가',values:leg.mid,color:'#5bdfb7',width:1.8},{label:last?'매수 1호가':'평균 매수 호가',values:leg.bid,color:'#78b4e8'},{label:last?'매도 1호가':'평균 매도 호가',values:leg.ask,color:'#f1b66e'}],{gapMs:milliseconds[config.interval]*Math.max(1,Math.ceil((r.meta.aligned_points||r.times.length)/Math.max(1,r.times.length)))});
      $('data-placeholder').hidden=leg.mid.some(Number.isFinite);$('data-csv').disabled=!r.times.length;
      $('data-table-time-label').textContent=hasQuoteTimes?(hasSizes?'채택 시각 (KST)':'원본 시각 (KST)'):'구간 시작 (KST)';
      const rows=[];for(let i=r.times.length-1;i>=Math.max(0,r.times.length-100);i--){
        const row=[time(displayTimes[i]),price(leg.mid[i]),quotePrice(leg.bid[i]),quotePrice(leg.ask[i]),Number.isFinite(leg.ask[i])&&Number.isFinite(leg.bid[i])?price(leg.ask[i]-leg.bid[i]):'—'];
        if(hasSizes)row.push(originalQuote(leg.bid_size[i]),originalQuote(leg.ask_size[i]));rows.push(row);
      }
      tableRows('data-table',rows,hasSizes?7:5,'선택한 구간에 조회 가능한 호가가 없습니다.');
      $('data-message').textContent=`${identity} · ${description} 조회 완료 · ${basis}`+((r.meta.warnings||[]).length?' · '+r.meta.warnings.join(' '):hasSizes?' · 1초 수집 저장분을 조회했습니다.':' · 완료된 시간별 파일의 과거 호가입니다.');
    }catch(error){if(currentQuery(version,selected)){clearData('조회하지 못했습니다. 조건을 확인한 뒤 다시 조회하세요.');errorAt('data',error);$('data-message').textContent='데이터를 조회하지 못했습니다.';}}
    finally{if(currentQuery(version,selected)){$('data-run').disabled=false;$('data-run').textContent='데이터 조회';}}
  }
  async function exportData() {
    if(!state.dataJob)return;const id=state.dataJob,version=state.versions.data,selection=state.selection;clearError('data');$('data-csv').disabled=true;
    try{
      const response=await fetch('/api/analyses/'+encodeURIComponent(id)+'/csv',{credentials:'same-origin',headers:{Accept:'text/csv'}});
      if(!currentQuery(version,selection)||state.dataJob!==id)return;
      if(response.status===401){location.assign('/login?next=%2F');throw new Error('로그인이 만료되었습니다. 다시 로그인해주세요.');}
      if(!response.ok){let details={};try{details=await response.json();}catch{}throw new Error(typeof details.error==='string'?details.error:'CSV를 내보내지 못했습니다. 다시 조회해주세요.');}
      const blob=await response.blob();if(!currentQuery(version,selection)||state.dataJob!==id)return;
      const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='oms-data.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){if(currentQuery(version,selection)&&state.dataJob===id)errorAt('data',error);}finally{if(currentQuery(version,selection)&&state.dataJob===id)$('data-csv').disabled=!state.dataJob||!state.data?.times.length;}
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
      ?'원본 호가의 모든 컬럼과 행을 시간순으로 내보냅니다. desktopTime은 원본 밀리초 타임스탬프입니다. USDT 1초 수집분은 실제 USDT 수량, 1초 기준·채택·거래소·수신 시각과 호가 나이를 포함합니다.'
      :'선택한 평균 간격의 전체 구간을 내보냅니다. 한국 시간, 거래소·종목·통화, 중간가·매수/매도 호가·스프레드·원본 행 수를 포함합니다. USDT 1초 수집분은 같은 구간의 평균 USDT 수량도 포함합니다.';
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
  for(const input of $('data-form').querySelectorAll('input'))input.addEventListener('input',changed);
  const controls=createHistoryControls({prefix:'data',api,getCatalog,onChanged:changed});
  state.selection=selectionKey();
  describePriceMode();
  return {ready:controls.ready,refresh:controls.refresh,onEnter(){dataChart.draw();}};
}
