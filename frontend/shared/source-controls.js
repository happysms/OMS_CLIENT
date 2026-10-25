import {$, exchanges, number, source, stamp, time, local} from './dom.js';

export function populateSource(prefix, getCatalog, {reset=false, allowedExchanges=null, catalogKey='exchanges'}={}) {
  const catalog=getCatalog();
  const select=$(prefix+'-exchange'), previous=select.value;
  const available=Object.keys(catalog[catalogKey]||{}).filter(exchange=>!allowedExchanges||allowedExchanges.has(exchange));
  select.replaceChildren(...available.map(exchange=>new Option(exchanges[exchange]||exchange,exchange)));
  if(available.includes(previous)) select.value=previous;
  else if(available.includes(catalog.defaults?.legs?.[0]?.exchange)) select.value=catalog.defaults.legs[0].exchange;
  else if(available.includes('bithumb')) select.value='bithumb';
  if(!$(prefix+'-ticker').value) $(prefix+'-ticker').value=catalog.defaults?.legs?.[0]?.ticker||'BTC';
  const tickers=catalog[catalogKey]?.[select.value]||[];
  populateTickers(prefix,getCatalog,reset||(catalogKey==='funding_exchanges'&&!tickers.includes($(prefix+'-ticker').value)),{catalogKey});
}

export function populateTickers(prefix,getCatalog,reset=false,{catalogKey='exchanges'}={}) {
  const {exchange}=source(prefix), tickers=getCatalog()[catalogKey]?.[exchange]||[];
  $(prefix+'-tickers').replaceChildren(...tickers.map(ticker=>new Option(ticker,ticker)));
  if(reset) $(prefix+'-ticker').value=tickers.includes('BTC')?'BTC':tickers[0]||'';
}

// Historical screens share instrument and time-range controls, not result state.
export function createHistoryControls({prefix,api,getCatalog,onChanged,dataType='price'}) {
  const catalogKey=dataType==='funding'?'funding_exchanges':'exchanges';
  if(dataType==='funding')$(prefix+'-ticker').dataset.preserveCase='true';
  const selection=()=>({...source(prefix),...(dataType==='funding'?{data_type:'funding'}:{})});
  let boundsVersion=0;
  async function bounds({setRange=false}={}) {
    const version=++boundsVersion, selected=selection();
    const previousRange={start:$(prefix+'-start').value,end:$(prefix+'-end').value};
    $(prefix+'-bounds').textContent='선택한 종목의 저장 범위를 확인하고 있습니다.';
    try {
      if(!selected.exchange||!selected.ticker)throw new Error('저장된 종목이 없습니다. 새 데이터 확인으로 목록을 갱신하세요.');
      const result=await api('/api/bounds?'+new URLSearchParams(selected));
      if(version!==boundsVersion||JSON.stringify(selected)!==JSON.stringify(selection())) return;
      const start=stamp(result.start),last=stamp(result.end),end=Math.ceil((last+1)/60000)*60000;
      if(!Number.isFinite(start)||!Number.isFinite(last)) throw new Error('이 종목의 저장 범위가 없습니다.');
      $(prefix+'-bounds').textContent=`저장 범위 ${time(start)} ~ ${time(last)} KST`;
      if(setRange&&$(prefix+'-start').value===previousRange.start&&$(prefix+'-end').value===previousRange.end){
        const nextStart=local(Math.max(start,end-86400000)),nextEnd=local(end);
        if(nextStart!==previousRange.start||nextEnd!==previousRange.end){
          $(prefix+'-start').value=nextStart;$(prefix+'-end').value=nextEnd;onChanged();
        }
      }
    } catch(error) {
      if(version===boundsVersion&&JSON.stringify(selected)===JSON.stringify(selection())) $(prefix+'-bounds').textContent=error.message;
    }
  }
  for(const input of $(prefix+'-form').querySelectorAll('input,select')) input.addEventListener('change',onChanged);
  for(const field of ['exchange','ticker']) $(prefix+'-'+field).addEventListener('change',()=>{
    if(field==='exchange') populateTickers(prefix,getCatalog,true,{catalogKey});
    else {const input=$(prefix+'-ticker'),typed=input.value.trim(),tickers=getCatalog()[catalogKey]?.[$(prefix+'-exchange').value]||[];input.value=tickers.find(t=>t.toUpperCase()===typed.toUpperCase())||typed.toUpperCase();}
    onChanged();
    bounds({setRange:true});
  });
  $(prefix+'-latest').addEventListener('click',()=>{onChanged();bounds({setRange:true});});
  function populate(){populateSource(prefix,getCatalog,{catalogKey});}
  populate();
  const defaults=getCatalog().defaults||{};
  if(defaults.start) $(prefix+'-start').value=local(stamp(defaults.start));
  if(defaults.end) $(prefix+'-end').value=local(stamp(defaults.end));
  return {bounds,refresh(){const previous=JSON.stringify(source(prefix));populate();const changed=previous!==JSON.stringify(source(prefix));if(changed)onChanged();return bounds({setRange:changed});},ready:bounds({setRange:true})};
}

export function renderCatalogSummary(catalog) {
  const count=Object.values(catalog.exchanges).reduce((sum,tickers)=>sum+tickers.length,0);
  $('catalog-summary').textContent=`${Object.keys(catalog.exchanges).length}개 거래소 · ${number(count,0)}개 거래소/종목`;
}
