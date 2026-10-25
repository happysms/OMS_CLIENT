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

export function errorAt(prefix, error) { $(prefix+'-error').textContent=error.message||String(error); $(prefix+'-error').hidden=false; }
export function clearError(prefix) { $(prefix+'-error').hidden=true; }
export function tableRows(id, rows, columns, empty) {
  const body=$(id); body.replaceChildren();
  if(!rows.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=columns;cell.className='empty-cell';cell.textContent=empty;row.append(cell);body.append(row);return;}
  for(const values of rows){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=value??'—';row.append(cell);}body.append(row);}
}
export function source(prefix) { return {exchange:$(prefix+'-exchange').value,ticker:$(prefix+'-ticker').value.trim().toUpperCase()}; }
export function range(prefix) {
  const start=$(prefix+'-start').value,end=$(prefix+'-end').value;
  if(!Number.isFinite(parseLocal(start))||!Number.isFinite(parseLocal(end))||parseLocal(start)>=parseLocal(end))throw new Error('시작보다 늦은 종료 시간을 입력해주세요. 시간은 KST 기준입니다.');
  return {start,end,interval:$(prefix+'-interval').value};
}
