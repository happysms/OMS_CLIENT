import {$,exchanges,number,price,stamp,time,errorAt,clearError,tableRows,source} from '../../shared/dom.js';
import {populateSource,populateTickers} from '../../shared/source-controls.js';

export function initPaperTrading({api,getCatalog}) {
  const state={paperLoaded:false,paperBusy:false,paperVersion:0};
  const paperExchanges=new Set(['bithumb','upbit']);
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
