import {$, exchanges, number, source, stamp, time, local} from './dom.js';

export function populateSource(prefix, getCatalog, {reset=false, allowedExchanges=null}={}) {
  const catalog=getCatalog();
  const select=$(prefix+'-exchange'), previous=select.value;
  const available=Object.keys(catalog.exchanges).filter(exchange=>!allowedExchanges||allowedExchanges.has(exchange));
  select.replaceChildren(...available.map(exchange=>new Option(exchanges[exchange]||exchange,exchange)));
  if(available.includes(previous)) select.value=previous;
  else if(available.includes(catalog.defaults?.legs?.[0]?.exchange)) select.value=catalog.defaults.legs[0].exchange;
  else if(available.includes('bithumb')) select.value='bithumb';
  if(!$(prefix+'-ticker').value) $(prefix+'-ticker').value=catalog.defaults?.legs?.[0]?.ticker||'BTC';
  populateTickers(prefix,getCatalog,reset);
}

export function populateTickers(prefix,getCatalog,reset=false) {
  const {exchange}=source(prefix), tickers=getCatalog().exchanges[exchange]||[];
  $(prefix+'-tickers').replaceChildren(...tickers.map(ticker=>new Option(ticker,ticker)));
  if(reset) $(prefix+'-ticker').value=tickers.includes('BTC')?'BTC':tickers[0]||'';
}

// Historical screens share instrument and time-range controls, not result state.
export function createHistoryControls({prefix,api,getCatalog,onChanged}) {
  let boundsVersion=0;
  async function bounds({setRange=false}={}) {
    const version=++boundsVersion, selection=source(prefix);
    $(prefix+'-bounds').textContent='선택한 종목의 저장 범위를 확인하고 있습니다.';
    try {
      const result=await api('/api/bounds?'+new URLSearchParams(selection));
      if(version!==boundsVersion) return;
      const start=stamp(result.start),last=stamp(result.end),end=Math.ceil((last+1)/60000)*60000;
      if(!Number.isFinite(start)||!Number.isFinite(last)) throw new Error('이 종목의 저장 범위가 없습니다.');
      $(prefix+'-bounds').textContent=`저장 범위 ${time(start)} ~ ${time(last)} KST`;
      if(setRange){$(prefix+'-start').value=local(Math.max(start,end-86400000));$(prefix+'-end').value=local(end);}
    } catch(error) {
      if(version===boundsVersion) $(prefix+'-bounds').textContent=error.message;
    }
  }
  for(const input of $(prefix+'-form').querySelectorAll('input,select')) input.addEventListener('change',onChanged);
  for(const field of ['exchange','ticker']) $(prefix+'-'+field).addEventListener('change',()=>{
    if(field==='exchange') populateTickers(prefix,getCatalog,true);
    else $(prefix+'-ticker').value=$(prefix+'-ticker').value.trim().toUpperCase();
    bounds({setRange:true});
  });
  $(prefix+'-latest').addEventListener('click',()=>{onChanged();bounds({setRange:true});});
  function populate(){populateSource(prefix,getCatalog);}
  populate();
  const defaults=getCatalog().defaults||{};
  if(defaults.start) $(prefix+'-start').value=local(stamp(defaults.start));
  if(defaults.end) $(prefix+'-end').value=local(stamp(defaults.end));
  return {bounds,refresh(){populate();return bounds();},ready:bounds({setRange:true})};
}

export function renderCatalogSummary(catalog) {
  const count=Object.values(catalog.exchanges).reduce((sum,tickers)=>sum+tickers.length,0);
  $('catalog-summary').textContent=`${Object.keys(catalog.exchanges).length}개 거래소 · ${number(count,0)}개 거래소/종목`;
}
