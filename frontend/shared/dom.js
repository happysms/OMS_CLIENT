export const $ = id => document.getElementById(id);
export const exchanges = {bithumb:'Bithumb',upbit:'Upbit',okx:'OKX · 무기한',binance:'Binance',hyperliquid:'Hyperliquid',ls:'LS'};
export const milliseconds = {'1s':1000,'10s':10000,'1min':60000,'5min':300000,'1h':3600000};
export const number = (v, digits=4) => Number.isFinite(v) ? new Intl.NumberFormat('en-US',{maximumFractionDigits:digits}).format(v) : '—';
export const currency = exchange => ['bithumb','upbit','ls'].includes(exchange) ? 'KRW' : 'USDT';
export const price = v => number(v, Math.abs(v)<10 ? 6 : 3);
export const stamp = value => typeof value==='number' ? value : Date.parse(value);
export const time = value => Number.isFinite(stamp(value)) ? new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(stamp(value)) : '—';
export const local = ms => new Date(ms+9*3600000).toISOString().slice(0,16);
export const parseLocal = value => Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value+'+09:00');
export const wait = ms => new Promise(resolve=>setTimeout(resolve,ms));

export function pnlCurrency(section, value) {
  const key='oms-pnl-currency-'+section;
  try {
    if(value!==undefined)localStorage.setItem(key,value==='USDT'?'USDT':'KRW');
    return (value??localStorage.getItem(key))==='USDT'?'USDT':'KRW';
  } catch {return value==='USDT'?'USDT':'KRW';}
}
export function pnlAccount(report, unit) {return (unit==='USDT'?report?.usdt?.account:report?.account)||{};}
export function pnlFill(report, unit) {return (unit==='USDT'?report?.usdt?.fill:report?.fill??report)||{};}
export function pnlTheoryPrice(report, unit) {
  const value=report?.fill?report.execution_theory?.price:(report?.theory?.price??report?.positions?.[0]?.theory_price);
  if(unit!=='USDT')return value;
  const rate=report?.usdt?.fill?.fx?.krw_per_usdt;
  return Number.isFinite(value)&&Number.isFinite(rate)&&rate>0?value/rate:null;
}
export function fxDescription(report, unit) {
  if(unit!=='USDT')return '손익·포지션·체결 금액: KRW. 주문 입력과 원본 호가·틱은 거래소 기준 통화입니다.';
  const fx=report?.usdt?.fx;
  return '손익·포지션·체결 금액: USDT. 체결 원가는 당시 환율, 보유자산 평가는 현재 환율을 적용합니다. '+
    (fx?`평가 환율 1 USDT = ${number(fx.krw_per_usdt,4)} KRW · ${time(fx.observed_at_ms)} KST`:'평가 환율 없음·지연: 계산할 수 없는 금액은 —')+
    ' · 매매 총손익 + 원화 현금 환산손익 = 초기 대비 평가액 변화. 주문 입력·원본 호가·틱의 통화는 그대로입니다.';
}

export function errorAt(prefix, error) { $(prefix+'-error').textContent=error.message||String(error); $(prefix+'-error').hidden=false; }
export function clearError(prefix) { $(prefix+'-error').hidden=true; }
export function tableRows(id, rows, columns, empty) {
  const body=$(id); body.replaceChildren();
  if(!rows.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=columns;cell.className='empty-cell';cell.textContent=empty;row.append(cell);body.append(row);return;}
  for(const values of rows){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=value??'—';row.append(cell);}body.append(row);}
}
export function source(prefix) { const input=$(prefix+'-ticker'),ticker=input.value.trim();return {exchange:$(prefix+'-exchange').value,ticker:input.dataset.preserveCase==='true'?ticker:ticker.toUpperCase()}; }
export function range(prefix) {
  const start=$(prefix+'-start').value,end=$(prefix+'-end').value;
  if(!Number.isFinite(parseLocal(start))||!Number.isFinite(parseLocal(end))||parseLocal(start)>=parseLocal(end))throw new Error('시작보다 늦은 종료 시간을 입력해주세요. 시간은 KST 기준입니다.');
  return {start,end,interval:$(prefix+'-interval').value};
}
