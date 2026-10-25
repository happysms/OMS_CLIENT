import {TimeChart} from './charts.js';

const $ = id => document.getElementById(id);
const exchanges = {bithumb:'Bithumb',upbit:'Upbit',okx:'OKX · 무기한',binance:'Binance',hyperliquid:'Hyperliquid',ls:'LS'};
const paperExchanges = new Set(['bithumb','upbit']);
const milliseconds = {'1s':1000,'10s':10000,'1min':60000,'5min':300000,'1h':3600000};
const number = (v, digits=4) => Number.isFinite(v) ? new Intl.NumberFormat('en-US',{maximumFractionDigits:digits}).format(v) : '—';
const currency = exchange => ['bithumb','upbit','ls'].includes(exchange) ? 'KRW' : 'USDT';
const price = v => number(v, Math.abs(v)<10 ? 6 : 3);
const stamp = value => typeof value==='number' ? value : Date.parse(value);
const time = value => Number.isFinite(stamp(value)) ? new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(stamp(value)) : '—';
const local = ms => new Date(ms+9*3600000).toISOString().slice(0,16);
const parseLocal = value => Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value+'+09:00');
const wait = ms => new Promise(resolve=>setTimeout(resolve,ms));

function errorAt(prefix, error) { $(prefix+'-error').textContent=error.message||String(error); $(prefix+'-error').hidden=false; }
function clearError(prefix) { $(prefix+'-error').hidden=true; }
function tableRows(id, rows, columns, empty) {
  const body=$(id); body.replaceChildren();
  if(!rows.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=columns;cell.className='empty-cell';cell.textContent=empty;row.append(cell);body.append(row);return;}
  for(const values of rows){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=value??'—';row.append(cell);}body.append(row);}
}
function source(prefix) { return {exchange:$(prefix+'-exchange').value,ticker:$(prefix+'-ticker').value.trim().toUpperCase()}; }
function range(prefix) {
  const start=$(prefix+'-start').value,end=$(prefix+'-end').value;
  if(!Number.isFinite(parseLocal(start))||!Number.isFinite(parseLocal(end))||parseLocal(start)>=parseLocal(end))throw new Error('시작보다 늦은 종료 시간을 입력해주세요. 시간은 KST 기준입니다.');
  return {start,end,interval:$(prefix+'-interval').value};
}

export async function initWorkspace({api,getCatalog,stopLive}) {
  const state={module:'analysis',versions:{data:0,backtest:0},boundsVersions:{data:0,backtest:0},bounds:{},data:null,dataJob:null,backtest:null,paperLoaded:false,paperBusy:false,paperVersion:0};
  const dataChart=new TimeChart($('data-chart'),{format:price,onHover:t=>dataChart.setCursor(t),onZoom:r=>dataChart.setZoom(r)});
  const backtestChart=new TimeChart($('backtest-chart'),{format:v=>number(v,2),onHover:t=>backtestChart.setCursor(t),onZoom:r=>backtestChart.setZoom(r)});

  function populateTickers(prefix,reset=false) {
    const {exchange}=source(prefix),list=$(prefix+'-tickers'),tickers=getCatalog().exchanges[exchange]||[];
    list.replaceChildren(...tickers.map(ticker=>new Option(ticker,ticker)));
    if(reset)$(prefix+'-ticker').value=tickers.includes('BTC')?'BTC':tickers[0]||'';
  }
  function populate() {
    const catalog=getCatalog();
    for(const prefix of ['data','backtest','paper']){
      const select=$(prefix+'-exchange'),previous=select.value,available=Object.keys(catalog.exchanges).filter(exchange=>prefix!=='paper'||paperExchanges.has(exchange));
      select.replaceChildren(...available.map(exchange=>new Option(exchanges[exchange]||exchange,exchange)));
      if(available.includes(previous))select.value=previous;
      else if(available.includes(catalog.defaults?.legs?.[0]?.exchange))select.value=catalog.defaults.legs[0].exchange;
      else if(available.includes('bithumb'))select.value='bithumb';
      const ticker=$(prefix+'-ticker');
      if(!ticker.value)ticker.value=catalog.defaults?.legs?.[0]?.ticker||'BTC';
      populateTickers(prefix);
    }
    const count=Object.values(catalog.exchanges).reduce((sum,tickers)=>sum+tickers.length,0);
    $('catalog-summary').textContent=`${Object.keys(catalog.exchanges).length}개 거래소 · ${number(count,0)}개 거래소/종목`;
  }

  async function bounds(prefix,{setRange=false}={}) {
    const version=++state.boundsVersions[prefix],selection=source(prefix);
    $(prefix+'-bounds').textContent='선택한 종목의 저장 범위를 확인하고 있습니다.';
    try{
      const result=await api('/api/bounds?'+new URLSearchParams(selection));
      if(version!==state.boundsVersions[prefix])return;
      const start=stamp(result.start),last=stamp(result.end),end=Math.ceil((last+1)/60000)*60000;
      if(!Number.isFinite(start)||!Number.isFinite(last))throw new Error('이 종목의 저장 범위가 없습니다.');
      state.bounds[prefix]={start,end};
      $(prefix+'-bounds').textContent=`저장 범위 ${time(start)} ~ ${time(last)} KST`;
      if(setRange){$(prefix+'-start').value=local(Math.max(start,end-86400000));$(prefix+'-end').value=local(end);}
    }catch(error){if(version===state.boundsVersions[prefix]){state.bounds[prefix]=null;$(prefix+'-bounds').textContent=error.message;}}
  }
  function changed(prefix) {
    state.versions[prefix]++;
    $(prefix+'-run').disabled=false;
    $(prefix+'-run').textContent=prefix==='data'?'데이터 조회':'백테스트 실행';
    $(prefix+'-message').textContent='설정이 바뀌었습니다. 실행하면 새 조건을 적용합니다.';
    if(prefix==='data'){$('data-csv').disabled=true;state.dataJob=null;}
  }
  async function job(path,body,prefix,version) {
    const created=await api(path,{method:'POST',body}),began=Date.now();
    for(;;){
      if(version!==state.versions[prefix])return null;
      const response=await api(path+'/'+encodeURIComponent(created.id));
      if(version!==state.versions[prefix])return null;
      if(['complete','succeeded'].includes(response.status)){
        if(!response.result)throw new Error('결과 보관 기간이 지났습니다. 같은 조건으로 다시 실행해주세요.');
        return {id:created.id,result:response.result};
      }
      if(['failed','cancelled','stopped'].includes(response.status))throw new Error(typeof response.error==='string'?response.error:response.error?.message||'서버 계산을 완료하지 못했습니다.');
      $(prefix+'-message').textContent=`${response.status==='queued'?'서버 작업 대기 중':'서버에서 계산 중'} · ${Math.floor((Date.now()-began)/1000)}초`;
      if(Date.now()-began>600000)throw new Error('계산 대기가 길어지고 있습니다. 잠시 후 다시 실행해주세요.');
      await wait(500);
    }
  }
  async function runData(event) {
    event.preventDefault();const version=++state.versions.data;clearError('data');$('data-run').disabled=true;$('data-run').textContent='조회 중…';$('data-csv').disabled=true;
    try{
      const selection=source('data'),config={...range('data'),legs:[{id:'A',...selection}],formula:'A',unit:currency(selection.exchange),ewm_span:1,max_points:6000};
      const response=await job('/api/analyses',config,'data',version);if(!response)return;
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
  async function runBacktest(event) {
    event.preventDefault();const version=++state.versions.backtest;clearError('backtest');$('backtest-run').disabled=true;$('backtest-run').textContent='계산 중…';
    try{
      const selection=source('backtest'),config={...selection,...range('backtest'),strategy:'ma_cross',fast_window:Number($('backtest-fast').value),slow_window:Number($('backtest-slow').value),initial_cash:Number($('backtest-cash').value),fee_bps:Number($('backtest-fee').value),slippage_bps:Number($('backtest-slippage').value)};
      if(!Number.isInteger(config.fast_window)||!Number.isInteger(config.slow_window)||config.fast_window<1||config.fast_window>=config.slow_window)throw new Error('단기 이동평균 구간은 장기 구간보다 작은 양의 정수여야 합니다.');
      const response=await job('/api/backtests',config,'backtest',version);if(!response)return;
      const r=response.result,m=r.metrics,unit=currency(selection.exchange);state.backtest=r;
      $('bt-return').textContent=number(m.return_pct,2)+' %';$('bt-final').textContent=`최종 평가액 ${number(m.final_equity,2)} ${unit}`;
      $('bt-drawdown').textContent=number(m.max_drawdown_pct,2)+' %';$('bt-trades').textContent=number(m.closed_trades??m.trades,0)+' 회';$('bt-winrate').textContent='승률 '+number(m.win_rate_pct,2)+' %';$('bt-fees').textContent=number(m.fees_paid,2);$('bt-currency').textContent=unit;
      const lines=[{label:'전략 자산',values:r.equity.values,color:'#5bdfb7',width:1.8}];if(Array.isArray(r.equity.benchmark))lines.push({label:'매수 후 보유',values:r.equity.benchmark,color:'#78b4e8',dashed:true});
      backtestChart.setZoom(null);backtestChart.setData(r.equity.times,lines);$('backtest-placeholder').hidden=r.equity.values.some(Number.isFinite);
      $('backtest-chart-label').textContent=`${exchanges[selection.exchange]||selection.exchange} ${selection.ticker} · ${unit} · ${config.fast_window}/${config.slow_window} 이동평균 · 민트: 전략${lines.length>1?' / 파랑: 매수 후 보유':''}`;
      const trades=r.trades||[];$('backtest-trade-count').textContent=number(trades.length,0)+' FILLS';
      tableRows('backtest-trades',trades.slice().reverse().map(trade=>[time(trade.timestamp??trade.time),trade.side==='buy'?'매수':'매도',price(trade.price),number(trade.quantity,8),number(trade.fee,4),number(trade.realized_pnl,4)]),6,'선택한 구간에 전략 체결이 없습니다.');
      $('backtest-assumptions-list').replaceChildren();for(const text of r.assumptions||[]){const item=document.createElement('li');item.textContent=text;$('backtest-assumptions-list').append(item);}$('backtest-assumptions').hidden=!(r.assumptions||[]).length;
      $('backtest-message').textContent=`백테스트 완료 · ${number(m.trades??trades.length,0)}회 체결 · 수수료 ${config.fee_bps}bp / 슬리피지 ${config.slippage_bps}bp 적용`;
    }catch(error){if(version===state.versions.backtest){errorAt('backtest',error);$('backtest-message').textContent='백테스트 조건을 확인해주세요.';}}
    finally{if(version===state.versions.backtest){$('backtest-run').disabled=false;$('backtest-run').textContent='백테스트 실행';}}
  }

  function renderPaper(snapshot) {
    const account=snapshot.account||{},positions=snapshot.positions||[],orders=snapshot.orders||[],unit=account.quote_currency||'KRW';
    const allMarked=positions.every(position=>Number.isFinite(position.market_value));
    const equity=Object.hasOwn(snapshot,'equity')?snapshot.equity:Number.isFinite(account.equity)?account.equity:allMarked&&Number.isFinite(account.cash)?account.cash+positions.reduce((sum,position)=>sum+position.market_value,0):null;
    const unrealized=positions.every(position=>Number.isFinite(position.unrealized_pnl))?positions.reduce((sum,position)=>sum+position.unrealized_pnl,0):null;
    $('paper-equity').textContent=number(equity,2);$('paper-cash').textContent=number(account.cash,2);$('paper-unit').textContent=unit+' · 모의 자산';$('paper-initial').textContent='초기 자금 '+number(account.initial_cash,2)+' '+unit;$('paper-realized').textContent=number(account.realized_pnl,2);$('paper-unrealized').textContent=number(unrealized,2);
    $('paper-position-count').textContent=number(positions.length,0)+' POSITIONS';$('paper-order-count').textContent=number(orders.length,0)+' ORDERS';
    tableRows('paper-positions',positions.map(position=>[`${exchanges[position.exchange]||position.exchange} / ${position.ticker}`,number(position.quantity,8),price(position.avg_price),price(position.mark_price),number(position.market_value,2),number(position.unrealized_pnl,2),position.quote_time?time(position.quote_time)+' KST':'시세 확인 불가']),7,'보유한 모의 포지션이 없습니다.');
    tableRows('paper-orders',orders.slice().sort((a,b)=>stamp(b.timestamp)-stamp(a.timestamp)).slice(0,100).map(order=>[time(order.timestamp),`${exchanges[order.exchange]||order.exchange} / ${order.ticker}`,order.side==='buy'?'매수':'매도',number(order.quantity,8),price(order.price),number(order.fee,4),number(order.realized_pnl,4),`서버 호가 · ${time(order.quote_time)} KST`]),8,'아직 모의 주문이 없습니다.');
    const quote=snapshot.quote_status||{};
    $('paper-message').textContent=(quote.message||'서버 모의 계좌 조회 완료')+(quote.updated_at?' · 확인 '+time(quote.updated_at)+' KST':'');
    $('paper-message').classList.toggle('quote-unavailable',['partial','unavailable'].includes(quote.state));
    $('paper-reset-currency').value=unit;
    $('paper-order-note').textContent=`${unit} 현물형 모의 계좌 · 수수료 ${number(account.fee_bps,2)}bp / 슬리피지 ${number(account.slippage_bps,2)}bp. 주문 시 서버가 새 호가를 확인합니다.`;
    state.paperLoaded=true;
  }
  async function loadPaper({clear=true}={}) {
    const version=++state.paperVersion;if(clear)clearError('paper');$('paper-refresh').disabled=true;
    try{const snapshot=await api('/api/paper');if(version===state.paperVersion)renderPaper(snapshot);}
    catch(error){if(version===state.paperVersion){errorAt('paper',error);$('paper-message').textContent='최신 잔고와 시세를 확인하지 못했습니다. 새로고침해주세요.';}}
    finally{if(version===state.paperVersion)$('paper-refresh').disabled=false;}
  }
  function paperBusy(value) {state.paperBusy=value;for(const id of ['paper-submit','paper-reset','paper-refresh'])$(id).disabled=value;}
  async function orderPaper(event) {
    event.preventDefault();if(state.paperBusy)return;clearError('paper');paperBusy(true);
    try{
      const quantity=Number($('paper-quantity').value);if(!Number.isFinite(quantity)||quantity<=0)throw new Error('0보다 큰 주문 수량을 입력해주세요.');
      await api('/api/paper/orders',{method:'POST',body:{...source('paper'),side:$('paper-side').value,quantity}});
      await loadPaper({clear:false});$('paper-quantity').value='';
    }catch(error){errorAt('paper',error);}finally{paperBusy(false);}
  }
  async function resetPaper(event) {
    event.preventDefault();if(state.paperBusy)return;clearError('paper');
    if(!$('paper-reset-confirm').checked){errorAt('paper',new Error('초기화 확인 항목을 선택해주세요.'));return;}
    paperBusy(true);
    try{
      const initial_cash=Number($('paper-reset-cash').value);if(!Number.isFinite(initial_cash)||initial_cash<=0)throw new Error('초기 자금은 0보다 커야 합니다.');
      await api('/api/paper/reset',{method:'POST',body:{initial_cash,quote_currency:$('paper-reset-currency').value}});$('paper-reset-confirm').checked=false;await loadPaper({clear:false});
    }catch(error){errorAt('paper',error);}finally{paperBusy(false);}
  }

  function selectModule(module) {
    if(state.module===module)return;
    const previous=state.module;state.module=module;
    for(const tab of document.querySelectorAll('[data-module]')){const selected=tab.dataset.module===module;tab.classList.toggle('active',selected);tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
    for(const panel of document.querySelectorAll('[role="tabpanel"]'))panel.hidden=panel.id!=='module-'+module;
    if(previous==='analysis')stopLive({quiet:true}).catch(()=>{});
    if(module==='data')dataChart.draw();if(module==='backtest')backtestChart.draw();if(module==='paper')loadPaper();
    try{history.replaceState(null,'','#'+module);}catch{/* Tabs remain usable in embedded contexts. */}
  }
  const tabs=[...document.querySelectorAll('[data-module]')];
  tabs.forEach((tab,index)=>{
    tab.addEventListener('click',()=>selectModule(tab.dataset.module));
    tab.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();tabs[next].focus();selectModule(tabs[next].dataset.module);});
  });
  for(const prefix of ['data','backtest']){
    for(const input of $(prefix+'-form').querySelectorAll('input,select'))input.addEventListener('change',()=>changed(prefix));
    for(const field of ['exchange','ticker'])$(prefix+'-'+field).addEventListener('change',()=>{if(field==='exchange')populateTickers(prefix,true);else $(prefix+'-ticker').value=$(prefix+'-ticker').value.trim().toUpperCase();bounds(prefix,{setRange:true});});
    $(prefix+'-latest').addEventListener('click',()=>{changed(prefix);bounds(prefix,{setRange:true});});
  }
  $('paper-exchange').addEventListener('change',()=>populateTickers('paper',true));$('paper-ticker').addEventListener('change',()=>$('paper-ticker').value=$('paper-ticker').value.trim().toUpperCase());
  $('paper-side').addEventListener('change',()=>$('paper-submit').textContent=$('paper-side').value==='buy'?'모의 매수 주문':'모의 매도 주문');
  $('data-form').addEventListener('submit',runData);$('data-csv').addEventListener('click',exportData);$('backtest-form').addEventListener('submit',runBacktest);$('backtest-reset-zoom').addEventListener('click',()=>backtestChart.setZoom(null));
  $('paper-refresh').addEventListener('click',()=>{if(!state.paperBusy)loadPaper();});$('paper-order-form').addEventListener('submit',orderPaper);$('paper-reset-form').addEventListener('submit',resetPaper);
  document.addEventListener('oms:catalog',()=>{populate();bounds('data');bounds('backtest');});
  populate();
  const defaults=getCatalog().defaults||{};
  for(const prefix of ['data','backtest']){if(defaults.start)$(prefix+'-start').value=local(stamp(defaults.start));if(defaults.end)$(prefix+'-end').value=local(stamp(defaults.end));}
  // Bounds are independent requests; an unavailable instrument in one module does not prevent the other from opening.
  await Promise.allSettled([bounds('data',{setRange:true}),bounds('backtest',{setRange:true})]);
  const initial=location.hash?.slice(1);if(tabs.some(tab=>tab.dataset.module===initial))selectModule(initial);
}
