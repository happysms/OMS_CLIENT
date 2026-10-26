import {$,exchanges,number,price,stamp,time,errorAt,clearError,tableRows,source} from '../../shared/dom.js';
import {populateSource,populateTickers} from '../../shared/source-controls.js';

export function initPaperTrading({api,getCatalog}) {
  const state={paperLoaded:false,paperBusy:false,paperVersion:0,fills:[],nextFill:null};
  const paperExchanges=new Set(['bithumb','upbit']);
  function renderPaper(snapshot) {
    const account=snapshot.account||{},positions=snapshot.positions||[],orders=snapshot.orders||[],unit=account.quote_currency||'KRW';
    const allMarked=positions.every(position=>Number.isFinite(position.market_value));
    const equity=Object.hasOwn(snapshot,'equity')?snapshot.equity:Number.isFinite(account.equity)?account.equity:allMarked&&Number.isFinite(account.cash)?account.cash+positions.reduce((sum,position)=>sum+position.market_value,0):null;
    const unrealized=positions.every(position=>Number.isFinite(position.unrealized_pnl))?positions.reduce((sum,position)=>sum+position.unrealized_pnl,0):null;
    $('paper-equity').textContent=number(equity,2);$('paper-cash').textContent=number(account.cash,2);$('paper-unit').textContent=unit+' · 모의 자산';$('paper-initial').textContent='초기 자금 '+number(account.initial_cash,2)+' '+unit;$('paper-realized').textContent=number(account.realized_pnl,2);$('paper-unrealized').textContent=number(unrealized,2);$('paper-theory-unrealized').textContent=number(account.theory_unrealized_pnl,2);$('paper-total-pnl').textContent=number(account.total_pnl,2);$('paper-theory-total').textContent=number(account.theory_total_pnl,2);
    $('paper-position-count').textContent=number(positions.length,0)+' POSITIONS';$('paper-order-count').textContent=number(snapshot.fill_count??orders.length,0)+' FILLS';
    tableRows('paper-positions',positions.map(position=>[`${exchanges[position.exchange]||position.exchange} / ${position.ticker}`,number(position.quantity,8),price(position.avg_price),price(position.mark_price),number(position.market_value,2),number(position.unrealized_pnl,2),price(position.theory_price),number(position.theory_unrealized_pnl,2),position.quote_time?time(position.quote_time)+' KST':'시세 확인 불가']),9,'보유한 모의 포지션이 없습니다.');
    state.fills=snapshot.fills||orders.map(fill=>({fill,account:null,valuation_status:'legacy_unavailable'}));
    state.nextFill=snapshot.next_fill_before??null;renderFills();
    const quote=snapshot.quote_status||{};
    $('paper-message').textContent=(quote.message||'서버 모의 계좌 조회 완료')+(quote.updated_at?' · 확인 '+time(quote.updated_at)+' KST':'');
    $('paper-message').classList.toggle('quote-unavailable',['partial','unavailable'].includes(quote.state));
    $('paper-reset-currency').value=unit;
    $('paper-order-note').textContent=`${unit} 현물형 모의 계좌 · 수수료 ${number(account.fee_bps,2)}bp / 슬리피지 ${number(account.slippage_bps,2)}bp. 주문 시 서버가 새 호가를 확인합니다.`;
    state.paperLoaded=true;
  }
  function renderFills(){
    tableRows('paper-orders',state.fills.map(report=>{
      const f=report.fill,a=report.account||{},position=report.positions?.find(p=>p.exchange===f.exchange&&p.ticker===f.ticker);
      return [time(f.timestamp),`${exchanges[f.exchange]||f.exchange} / ${f.ticker}`,f.side==='buy'?'매수':'매도',number(f.quantity,8),price(f.price),number(f.fee,4),number(f.realized_pnl,4),number(a.realized_pnl,4),number(a.unrealized_pnl,4),number(a.theory_unrealized_pnl,4),number(a.total_pnl,4),number(a.theory_total_pnl,4),number(a.cash??f.cash,2),number(a.equity,2),number(position?.quantity??(report.account?0:null),8),price(report.execution_theory?.price),report.valuation_status==='legacy_unavailable'?'과거 평가 기록 없음':a.unrealized_pnl===null?'시장가 평가 불가':a.theory_unrealized_pnl===null?'이론가 평가 불가':'기록됨'];
    }),17,'아직 모의 체결이 없습니다.');
    $('paper-fills-more').hidden=state.nextFill===null;
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

  $('paper-fills-more').addEventListener('click',async()=>{
    const version=state.paperVersion,before=state.nextFill;if(before===null)return;$('paper-fills-more').disabled=true;
    try{const result=await api(`/api/paper/fills?before=${before}`);if(version!==state.paperVersion)return;state.fills.push(...result.items);state.nextFill=result.next_before;renderFills();}catch(error){errorAt('paper',error);}finally{$('paper-fills-more').disabled=false;}
  });
  $('paper-fills-csv').addEventListener('click',async()=>{
    try{const blob=await api('/api/paper/fills/csv',{responseType:'blob'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='paper-fills.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){errorAt('paper',error);}
  });
  function populate(){populateSource('paper',getCatalog,{allowedExchanges:paperExchanges});}
  $('paper-exchange').addEventListener('change',()=>populateTickers('paper',getCatalog,true));
  $('paper-ticker').addEventListener('change',()=>$('paper-ticker').value=$('paper-ticker').value.trim().toUpperCase());
  $('paper-side').addEventListener('change',()=>$('paper-submit').textContent=$('paper-side').value==='buy'?'모의 매수 주문':'모의 매도 주문');
  $('paper-refresh').addEventListener('click',()=>{if(!state.paperBusy)loadPaper();});
  $('paper-order-form').addEventListener('submit',orderPaper);
  $('paper-reset-form').addEventListener('submit',resetPaper);
  populate();
  return {refresh:populate,onEnter(){loadPaper();}};
}
