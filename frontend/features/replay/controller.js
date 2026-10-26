const $=id=>document.getElementById('replay-'+id);
const num=value=>value===null||value===undefined||!Number.isFinite(Number(value))?'—':Number(value).toLocaleString('ko-KR',{maximumFractionDigits:8});
const stamp=value=>value===null||value===undefined?'—':new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3,hour12:false}).format(new Date(value));
const local=value=>new Date(value+9*3600000).toISOString().slice(0,19);
const side=value=>value==='buy'?'매수':'매도';
const states={in_flight:'접수 중',live:'대기',cancel_pending:'취소 중',filled:'체결 완료',cancelled:'취소 완료',rejected:'거절',loading:'원본 로딩',running:'재생 중',paused:'일시정지',finished:'완료',stopped:'중지',failed:'실패',interrupted:'서버 중단'};
const guardReasons={waiting_sample:'시각 표본 대기',healthy:'정상',disabled:'보호 비활성',missing_timestamp:'시각 누락·단위 미확인',negative_delay:'음수 시간차',delay_limit:'시간차 상한 초과',delay_spike:'시간차 급증',above_resume:'재개 기준 초과',recovering:'정상 시세 유지 확인 중',stale_sample:'호가 갱신 지연'};
const terminal=s=>['finished','stopped','failed','interrupted'].includes(s);
const cell=value=>{const td=document.createElement('td');td.textContent=String(value??'—');return td;};
function row(values,className=''){const tr=document.createElement('tr');tr.className=className;tr.append(...values.map(cell));return tr;}
function rows(target,items,columns,empty='아직 데이터가 없습니다.'){
  if(items.length)target.replaceChildren(...items);
  else {const tr=row([empty]);tr.firstChild.colSpan=columns;tr.firstChild.className='empty-cell';target.replaceChildren(tr);}
}

// Outside orders are deliberately separate, above the ten recorded market rows.
export function bookView(book,orders=[]){
  const outside=[],levels=[];
  for(const [direction,field] of [['sell','asks'],['buy','bids']]){
    const market=book?.[field]||[],mine=orders.filter(o=>o.side===direction);
    const low=Math.min(...market.map(x=>x.price)),high=Math.max(...market.map(x=>x.price));
    const inside=[];
    for(const order of mine){if(!market.length||order.price<low||order.price>high)outside.push(order);else inside.push(order);}
    const prices=[...new Set([...market.map(x=>x.price),...inside.map(o=>o.price)])].sort((a,b)=>b-a);
    for(const price of prices){levels.push({side:direction,price,market:market.find(x=>x.price===price),orders:inside.filter(o=>o.price===price)});}
  }
  return {outside,levels};
}

export function initReplay({api,getCatalog}){
  let snapshot=null,id=null,visible=false,timer=null,polling=false,epoch=0,boundsEpoch=0,creating=false,controlTail=Promise.resolve();
  let historicalFills=null,nextBefore=null,historyCursor=null,historyEpoch=0;
  let strategyItems=[],strategyExtras=[],strategyEpoch=0;
  const strategyKey=item=>item.id+'@'+item.revision;
  const selectedStrategy=()=>[...strategyItems,...strategyExtras].find(item=>strategyKey(item)===$('strategy').value);
  function updateRunButton(){const strategy=selectedStrategy();$('run').disabled=creating||Boolean(snapshot&&!terminal(snapshot.status))||!strategy||strategy.archived||!strategy.executable;}
  function strategyHelp(){const item=selectedStrategy();$('strategy-help').textContent=item?`${item.name} · v${item.revision} · ${item.archived?'보관한 전략입니다. 복원 후 사용하세요.':item.execution_note}${item.guard_configured===false?' · 이전 버전: 핑 보호 미설정, 새 버전 저장 시 적용':''}`:'전략 관리에서 실행 가능한 전략을 등록하세요.';updateRunButton();}
  function strategyOptions(items=strategyItems,preferred=$('strategy').value){
    strategyItems=items;
    const choices=new Map(items.map(item=>[strategyKey(item),item]));
    strategyExtras=strategyExtras.map(item=>({...item,archived:items.find(latest=>latest.id===item.id)?.archived??item.archived}));
    for(const item of strategyExtras)if(!choices.has(strategyKey(item)))choices.set(strategyKey(item),item);
    $('strategy').replaceChildren(...[...choices.values()].map(item=>{const option=new Option(`${item.name} · v${item.revision}${item.archived?' · 보관됨':!item.executable?' · 실행 연결 필요':''}`,strategyKey(item));option.disabled=item.archived;return option;}));
    if(choices.has(preferred))$('strategy').value=preferred;
    else{const first=[...choices.values()].find(item=>item.executable&&!item.archived);if(first)$('strategy').value=strategyKey(first);}
    strategyHelp();
  }
  function useStrategy(item){
    if(!item)return;
    strategyExtras=strategyExtras.filter(old=>strategyKey(old)!==strategyKey(item));strategyExtras.push(item);
    strategyOptions(strategyItems,strategyKey(item));
  }
  async function loadStrategies(){const version=++strategyEpoch;const result=await api('/api/strategies');if(version===strategyEpoch)strategyOptions(result.items);}
  document.addEventListener('oms:strategies',event=>{strategyEpoch++;strategyOptions(event.detail);});
  document.addEventListener('oms:select-strategy',event=>useStrategy(event.detail));
  $('strategy').addEventListener('change',strategyHelp);

  function error(value){$('error').hidden=!value;$('error').textContent=value||'';}
  function populate(){const tickers=getCatalog()?.exchanges?.bithumb||[];$('tickers').replaceChildren(...tickers.map(t=>new Option(t,t)));}
  async function bounds(){
    const ticker=$('ticker').value.trim().toUpperCase();$('ticker').value=ticker;
    if(!ticker)return;
    const version=++boundsEpoch,previousStart=$('start').value,previousEnd=$('end').value;
    try{
      const result=await api('/api/bounds?'+new URLSearchParams({exchange:'bithumb',ticker}));
      if(version!==boundsEpoch||ticker!==$('ticker').value.trim().toUpperCase())return;
      const start=Date.parse(result.start),last=Date.parse(result.end);
      if(!Number.isFinite(start)||!Number.isFinite(last))throw new Error('저장된 호가 범위가 없습니다.');
      $('bounds').textContent=`저장 범위 ${stamp(start)} ~ ${stamp(last)} KST · 틱은 실행 시 함께 확인합니다.`;
      const completeHour=Math.floor(last/3600000)*3600000,end=completeHour>start?completeHour:Math.floor(last/1000)*1000;
      if(previousStart===$('start').value&&previousEnd===$('end').value){$('start').value=local(Math.max(start,end-60000));$('end').value=local(end);}
    }catch(e){if(version===boundsEpoch)$('bounds').textContent=e.message;}
  }
  function renderBook(){
    const book=snapshot.books?.bithumb,{outside,levels}=bookView(book,snapshot.orders);
    $('book-title').textContent=`빗썸 ${snapshot.config.ticker} · 5호가`;
    $('book-time').textContent=book?`${stamp(book.time)}${snapshot.virtual_time_ms-book.time>(snapshot.config.strategy?.parameters?.max_quote_age_ms??1000)?' · 지연 시세':''}`:'호가 대기';
    $('outside').hidden=!outside.length;
    rows($('outside-rows'),outside.map(o=>row([side(o.side),num(o.price),num(o.remaining),`${states[o.status]} · ${o.queue_known?'대기열 추정':'대기열 미확인'}`],'replay-own')),4);
    rows($('book'),levels.map(level=>{
      const own=level.orders.map(o=>`${num(o.remaining)} (${states[o.status]})`).join(' + ');
      const tr=row([side(level.side)+(level.market?'':' · 내 가격'),num(level.price),num(level.market?.quantity),own||'—'],`replay-${level.side}${own?' replay-own':''}`);
      tr.dataset.market=String(Boolean(level.market));return tr;
    }),4,'유효한 원본 호가를 기다리고 있습니다.');
    $('okx-panel').hidden=!snapshot.config.venues.includes('okx');
    const okx=snapshot.books?.okx;
    $('okx-time').textContent=okx?`${stamp(okx.time)}${snapshot.virtual_time_ms-okx.time>1000?' · 지연 시세':''}`:'호가 대기';
    rows($('okx-book'),bookView(okx).levels.map(l=>row([side(l.side),num(l.price),num(l.market?.quantity)],`replay-${l.side}`)),3);
  }
  function renderFills(){
    const fills=historicalFills||snapshot.fills||[];
    rows($('fills'),fills.map(f=>row([stamp(f.timestamp),f.order_id,side(f.side),num(f.price),num(f.quantity),num(f.fee),num(f.realized_pnl),num(f.account.realized_pnl),num(f.account.unrealized_pnl),num(f.account.theory_unrealized_pnl),num(f.account.total_pnl),num(f.account.equity),num(f.theory?.price??f.positions?.[0]?.theory_price),stamp(f.theory?.available_at_ms??f.positions?.[0]?.theory?.available_at_ms)])),14,'아직 모의 체결이 없습니다.');
    if(!historicalFills)nextBefore=fills.length===100?fills.at(-1).sequence:null;
    $('older').hidden=!nextBefore;$('newest').hidden=!historicalFills;
    $('fill-count').textContent=`${num(snapshot.fill_count)}건 · ${historicalFills?'과거 체결 페이지':'최근 체결'}`;
  }
  function render(next){
    if(next.id!==id||(snapshot&&next.revision<snapshot.revision))return;
    const justFinished=snapshot&&!terminal(snapshot.status)&&terminal(next.status);
    snapshot=next;
    const active=!terminal(next.status),loaded=active&&next.status!=='loading';
    updateRunButton();
    $('pause').disabled=!loaded;$('stop').disabled=!active;$('mm-toggle').disabled=!loaded;$('order').disabled=!loaded||Boolean(next.order_guard?.blocked);
    $('pause').textContent=next.paused?'재개':'일시정지';$('mm-toggle').textContent=next.config.auto_mm?'MM 중지':'MM 시작';
    $('csv').disabled=!next.fill_count;
    $('result').disabled=false;$('copy').disabled=active;
    const identity=next.virtual_account;
    $('account-info').textContent=identity?`${identity.name} · 계좌 ${identity.id} · 초기 ${num(identity.initial_krw)}원 / ${num(identity.initial_coin)} ${identity.ticker} · ${terminal(next.status)?(next.status==='finished'?'완료 결과 저장됨':'중단 시점까지 저장됨'):'계좌·체결·손익 자동 저장 중'}`:'이전 실행의 계좌 기록';
    const used=next.initial_config?.strategy||next.config.strategy;
    $('account-info').textContent+=used?` · 사용 전략 ${used.name} v${used.revision}`:' · 사용 전략 단일 거래소 메이커 (이전 실행)';
    // Do not move a slider while the user is dragging it.
    if(document.activeElement!==$('speed'))$('speed').value=next.speed;
    $('speed-label').textContent=`${next.speed}×`;$('fast').checked=next.fast;
    const elapsed=Math.max(0,next.virtual_time_ms-next.config.start_ms),duration=next.config.end_ms-next.config.start_ms;
    $('progress').value=Math.min(100,elapsed/duration*100);
    $('status').textContent=`${states[next.status]||next.status} · ${next.config.ticker} · ${stamp(next.virtual_time_ms)} KST · ${next.fast?'빠른 일괄 실행':next.speed+'×'} · ${num(next.event_count)}/${num(next.total_events)} 이벤트 · 제외 ${num(next.skipped_events)}${next.lag_ms>250?' · 처리 지연 '+num(next.lag_ms)+'ms':''}${next.error?' · '+next.error:''}`;
    const guard=next.order_guard;
    $('guard-status').textContent=guard?`${guard.enabled?(guard.blocked?'신규 주문 차단':'신규 주문 허용'):'핑 보호 비활성'} · ${guardReasons[guard.reason]||guard.reason} · 시세 시간차 ${num(guard.delta_ms)}ms · 직전 대비 ${num(guard.jump_ms)}ms${guard.recovery_remaining_ms!==null?' · 안정 확인 잔여 '+num(guard.recovery_remaining_ms)+'ms':''} · 차단된 주문 시도 ${num(guard.blocked_submissions)}건`:'이전 실행 · 핑 보호 기록 없음';
    rows($('guard-events'),(next.guard_events||[]).map(event=>row([stamp(event.timestamp),event.blocked?'차단':'허용',guardReasons[event.reason]||event.reason,num(event.delta_ms),num(event.jump_ms)])),5,'아직 차단·재개 기록이 없습니다.');
    const a=next.account||{};
    const metrics=[['평가액 · 원',a.equity],['주문 가능 원화',a.free_cash],['예약 원화',(a.cash??0)-(a.free_cash??0)],['보유 코인',a.coin],['주문 가능 코인',a.free_coin],['실현손익 · 원',a.realized_pnl],['미실현손익 · 원',a.unrealized_pnl],['이론가 미실현 · 원',a.theory_unrealized_pnl],['총손익 · 원',a.total_pnl]];
    $('stats').replaceChildren(...metrics.map(([title,value])=>{const article=document.createElement('article'),label=document.createElement('span'),strong=document.createElement('strong');label.textContent=title;strong.textContent=num(value);article.append(label,strong);return article;}));
    const theory=next.theory;
    $('theory-info').textContent=theory?`전략 이론가 ${num(theory.price)}원 · ${stamp(theory.available_at_ms)} KST · ${theory.formula||theory.basis}`:'전략 이론가 미평가 · 전략이 값을 제공하지 않았거나 유효시간이 지났습니다.';
    renderBook();
    rows($('ticks'),(next.ticks||[]).map(t=>row([stamp(t.time),t.venue==='bithumb'?'빗썸 · KRW':'OKX · USDT',t.direction_known?(t.qty>0?'매수 체결':'매도 체결'):'방향 미확인',num(t.price),num(Math.abs(t.qty))],t.direction_known?(t.qty>0?'replay-buy':'replay-sell'):'')),5);
    $('order-count').textContent=`${next.orders.length}개 미체결 주문`;
    rows($('orders'),next.orders.map(o=>{
      const tr=row([o.id,o.owner==='manual'?'수동':'MM',side(o.side),num(o.price),num(o.remaining),states[o.status],o.queue_known?num(o.queue_ahead):'미확인']);
      const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='small-button';button.textContent='취소';button.dataset.cancel=o.id;button.disabled=!loaded||o.status==='cancel_pending';td.append(button);tr.append(td);return tr;
    }),8,'미체결 주문이 없습니다.');
    const reasons={requested:'취소 요청',trade_tick:'시장 틱에 의한 체결',post_only_cross:'도착 시 반대 호가와 교차',self_cross:'내 반대 주문과 교차',stale_book:'호가 지연',end_of_run:'기간 종료',stopped:'실행 중지',failed:'실행 실패'};
    rows($('order-events'),(next.order_events||[]).map(o=>row([stamp(o.closed_at),o.id,states[o.status],o.reason?.startsWith('ping_guard:')?'시세 시간차 보호: '+(guardReasons[o.reason.slice(11)]||o.reason.slice(11)):reasons[o.reason]||o.reason])),4);
    renderFills();
    if(justFinished)history().catch(e=>error(e.message));
  }
  async function poll(){
    if(!visible||!id||polling)return;
    polling=true;const version=epoch,selected=id;
    try{const result=await api('/api/replays/'+selected);if(version===epoch)render(result);}catch(e){if(version===epoch)error(e.message);}finally{polling=false;if(version!==epoch&&visible&&id)poll();}
  }
  function schedule(){clearInterval(timer);if(visible)timer=setInterval(()=>{if(id&&(!snapshot||!terminal(snapshot.status)))poll();},250);}
  async function select(identifier){epoch++;id=identifier;snapshot=null;historicalFills=null;error('');await poll();schedule();}
  async function history(restore=false,append=false){
    if(append&&!historyCursor)return;
    const version=++historyEpoch,cursor=append?historyCursor:null;
    $('history-more').disabled=true;
    try{
      const list=await api('/api/replays'+(cursor?'?before='+cursor:''));
      if(version!==historyEpoch)return;
      const elements=list.items.map(item=>{
        const metadata=item.virtual_account||{},account=item.account||{};
        const tr=row(['',`${item.config.strategy?item.config.strategy.name+' v'+item.config.strategy.revision+' / ':''}${item.config.ticker} · ${stamp(item.config.start_ms)}`,states[item.status]||item.status,
          `${num(metadata.initial_krw)}원 / ${num(metadata.initial_coin)}개`,num(item.fill_count),num(account.equity),num(account.realized_pnl),num(account.unrealized_pnl),num(account.total_pnl)]);
        const button=document.createElement('button');button.type='button';button.className='quiet-button';button.textContent=metadata.name||item.config.ticker+' 모의계좌';button.title=metadata.id||item.id;button.addEventListener('click',()=>select(item.id));tr.firstChild.append(button);return tr;
      });
      if(append)$('history').append(...elements);else rows($('history'),elements,9,'저장된 모의계좌가 없습니다.');
      historyCursor=list.next_before||null;$('history-more').hidden=!historyCursor;
      if(restore&&!id&&list.items.length)await select((list.items.find(item=>!terminal(item.status))||list.items[0]).id);
    }finally{if(version===historyEpoch)$('history-more').disabled=false;}
  }
  function control(payload){
    const selected=id,version=epoch;
    controlTail=controlTail.catch(()=>{}).then(async()=>{
      if(selected!==id||version!==epoch)return;
      try{error('');const next=await api('/api/replays/'+selected+'/control',{method:'POST',body:payload});if(version===epoch)render(next);}catch(e){if(version===epoch)error(e.message);}
    });
    return controlTail;
  }
  $('form').addEventListener('submit',async event=>{
    event.preventDefault();if(creating)return;const strategy=selectedStrategy();if(!strategy||strategy.archived||!strategy.executable){error('실행 가능한 등록 전략을 선택하세요.');return;}creating=true;$('run').disabled=true;error('');
    const body={strategy_id:strategy.id,strategy_revision:strategy.revision,account_name:$('account-name').value.trim(),ticker:$('ticker').value.trim().toUpperCase(),start:$('start').value,end:$('end').value,initial_krw:$('cash').value,initial_coin:$('coin').value,fee_bps:$('fee').value,quantity_step:$('step').value,min_notional:$('minimum').value,latency_ms:Number($('latency').value),speed:Number($('speed').value),fast:$('fast').checked,include_okx:$('okx').checked,auto_mm:$('mm').checked};
    try{const result=await api('/api/replays',{method:'POST',body});epoch++;id=result.id;snapshot=null;historicalFills=null;render(result);schedule();await history();}catch(e){error(e.message);}finally{creating=false;updateRunButton();}
  });
  $('speed').addEventListener('input',()=>{$('speed-label').textContent=$('speed').value+'×';});
  for(const name of ['speed','fast'])$(name).addEventListener('change',()=>{if(snapshot&&!terminal(snapshot.status))control({action:'pace',speed:Number($('speed').value),fast:$('fast').checked});});
  $('pause').addEventListener('click',()=>control({action:snapshot?.paused?'play':'pause'}));
  $('stop').addEventListener('click',()=>control({action:'stop'}));
  $('mm-toggle').addEventListener('click',()=>control({action:'auto_mm',enabled:!snapshot?.config.auto_mm}));
  $('order-form').addEventListener('submit',event=>{event.preventDefault();control({action:'order',side:$('side').value,price:$('price').value,quantity:$('quantity').value});});
  $('orders').addEventListener('click',event=>{const button=event.target.closest('[data-cancel]');if(button)control({action:'cancel',order_id:button.dataset.cancel});});
  $('ticker').addEventListener('change',bounds);$('latest').addEventListener('click',bounds);
  $('copy').addEventListener('click',async()=>{
    if(!snapshot)return;
    const version=epoch;
    const original=snapshot.initial_config||snapshot.config;
    if(original.strategy)useStrategy(original.strategy);
    else{try{const originalMaker=await api('/api/strategies/builtin_single_maker?revision=1');if(version!==epoch)return;useStrategy(originalMaker);}catch(e){if(version===epoch)error(e.message);return;}}
    boundsEpoch++;
    for(const [field,key] of Object.entries({ticker:'ticker',cash:'initial_krw',coin:'initial_coin',fee:'fee_bps',step:'quantity_step',minimum:'min_notional',latency:'latency_ms',speed:'speed'}))$(field).value=original[key]??'';
    $('start').value=local(original.start_ms);$('end').value=local(original.end_ms);
    $('okx').checked=original.venues.includes('okx');$('mm').checked=original.auto_mm;$('fast').checked=original.fast;
    $('speed-label').textContent=original.speed+'×';$('account-name').value=((snapshot.virtual_account?.name||original.ticker+' 모의계좌')+' 복사').slice(0,80);
    $('bounds').textContent='선택한 계좌의 초기조건을 복사했습니다. 시작 버튼을 누르면 별도 계좌를 생성합니다.';
    if(original.theory_price)error('이전 고정 이론가는 복사하지 않습니다. 이번 실행은 선택한 전략의 이론가 출력을 사용합니다.');
    $('account-name').focus();
  });
  $('result').addEventListener('click',async()=>{
    const selected=id;$('result').disabled=true;
    try{const blob=await api(`/api/replays/${selected}/result.json`,{responseType:'blob'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`replay-${selected}-result.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error(e.message);}finally{$('result').disabled=!id;}
  });
  $('history-more').addEventListener('click',()=>history(false,true).catch(e=>error(e.message)));
  $('history-refresh').addEventListener('click',()=>history().catch(e=>error(e.message)));
  $('older').addEventListener('click',async()=>{
    const version=epoch,selected=id,before=nextBefore;if(!before)return;$('older').disabled=true;
    try{const result=await api(`/api/replays/${selected}/fills?before=${before}`);if(version!==epoch)return;historicalFills=result.items;nextBefore=result.next_before;renderFills();}catch(e){if(version===epoch)error(e.message);}finally{$('older').disabled=false;}
  });
  $('newest').addEventListener('click',()=>{historicalFills=null;renderFills();});
  $('csv').addEventListener('click',async()=>{
    const selected=id;try{const blob=await api(`/api/replays/${selected}/fills.csv`,{responseType:'blob'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`replay-${selected}-fills.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error(e.message);}
  });
  populate();updateRunButton();
  return {ready:Promise.resolve(),refresh:populate,onEnter(){visible=true;loadStrategies().catch(e=>error(e.message));history(true).catch(e=>error(e.message));poll();schedule();},onLeave(){visible=false;clearInterval(timer);}};
}
