const $ = id => document.getElementById('manual-' + id);
const states = {sending:'전송 중·상태 확인 필요',unknown:'접수 미확인',accepted:'접수·체결 확인 필요',live:'미체결',partial:'부분체결',filled:'전량체결',completed:'처리 완료',cancel_requested:'취소 요청·확인 필요',cancelled:'취소 완료',rejected:'접수 거절'};
const terminal = new Set(['filled','completed','cancelled','rejected']);
const stamp = value => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(value) : '—';
const side = value => value === 'buy' ? '매수' : '매도';
const row = values => {const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value??'—';tr.append(td);}return tr;};
const button = (label, action) => {const node=document.createElement('button');node.type='button';node.className='small-button';node.textContent=label;node.addEventListener('click',action);return node;};

// Compare positive decimal text without rounding adjacent prices to the same JS float.
export function comparePrice(left, right) {
  const [a, af=''] = String(left).split('.'), [b, bf=''] = String(right).split('.');
  if(a.length!==b.length)return a.length-b.length;
  if(a!==b)return a<b?-1:1;
  const length=Math.max(af.length,bf.length),x=af.padEnd(length,'0'),y=bf.padEnd(length,'0');
  return x===y?0:x<y?-1:1;
}

export function initManualTrading({api,allowDirectTrading=false}) {
  if(!allowDirectTrading){
    document.querySelector("#module-manual .manual-key-panel").hidden=true;
    for(const input of $("order-form").querySelectorAll("input,select,button"))input.disabled=true;
    $("order-status").textContent="실제 매매는 별도 실행 서버에서 수행합니다. 현재 공개 시세 조회만 가능하며 원격 주문·파라미터 제어 소켓은 연결 전입니다.";
  }
  let visible=false,timer=null,loading=false,version=0,busy=false,snapshot=null,pending=null;
  const error=value=>{$('error').hidden=!value;$('error').textContent=value||'';};
  const exchange=()=>$('exchange').value;
  const ticker=()=>$('ticker').value.trim().toUpperCase();
  const selectedKey=()=>snapshot?.keys.find(key=>key.id===$('key-id').value);
  function controls(){
    const key=selectedKey(),market=snapshot?.market;
    const ready=allowDirectTrading&&key?.verified_ms&&market?.can_order&&market.ticker===ticker()&&market.exchange===exchange();
    $('submit').disabled=busy||!ready||!$('confirm').checked||Boolean(pending?.unresolved);
    $('order-help').textContent=!allowDirectTrading?'원격 실행 서버 연결 전입니다. 이 PC에서는 거래소 키와 실제 주문을 처리하지 않습니다.':ready?'지정가 GTC · 가격×수량과 별도로 거래 수수료가 적용됩니다. 실제 주문임을 선택한 뒤 전송하세요.':'해당 거래소의 키 연결 확인과 2초 이내 호가가 필요합니다. 공개 시세만 연결해 볼 수도 있습니다.';
    for(const id of ['key-save','connect'])$(id).disabled=busy||(id==='key-save'&&!allowDirectTrading);
  }
  async function command(action){
    if(busy)return;
    busy=true;error('');controls();
    try{await action();await refresh();}catch(e){error(e.message);}finally{busy=false;controls();}
  }
  function renderKeys(){
    const previous=$('key-id').value;
    $('key-id').replaceChildren(...snapshot.keys.filter(key=>key.exchange===exchange()).map(key=>new Option(`${key.label} · ${key.key_hint}${key.verified_ms?'':' · 연결 확인 필요'}`,key.id)));
    if([...$('key-id').options].some(option=>option.value===previous))$('key-id').value=previous;
    $('keys').replaceChildren(...(allowDirectTrading?snapshot.keys:[]).map(key=>{
      const tr=row([key.exchange,key.label,key.key_hint,stamp(key.verified_ms),'']);
      tr.lastChild.append(button('잔고 조회·연결 확인',()=>command(async()=>{
        const data=await api(`/api/manual/keys/${key.id}/verify`,{method:'POST',body:{}});
        $('balances').replaceChildren(...data.balances.map(item=>row([item.currency,item.available,item.reserved])));
        $('key-status').textContent=`${key.label} · 잔고 조회 성공 · ${stamp(Date.now())} KST`;
      })),button('키 삭제',()=>command(async()=>{
        await api(`/api/manual/keys/${key.id}`,{method:'DELETE'});
        $('balances').replaceChildren();$('key-status').textContent='키를 삭제했습니다. 거래소의 다른 주문은 취소되지 않습니다.';
      })));
      return tr;
    }));
  }
  function mine(){return (snapshot?.orders||[]).filter(order=>order.key_id===$('key-id').value&&order.exchange===exchange()&&order.ticker===ticker()&&!terminal.has(order.status));}
  function priceCell(tr, price){
    tr.cells[1].replaceChildren(button(price,()=>{$('price').value=price;$('confirm').checked=false;controls();}));
  }
  function renderBook(){
    const book=snapshot.market.book,levels=[],outside=[];
    for(const [direction,field] of [['sell','asks'],['buy','bids']]){
      const depth=book?.[field]||[],orders=mine().filter(order=>order.side===direction);
      const low=depth.reduce((v,item)=>v===null||comparePrice(item[0],v)<0?item[0]:v,null);
      const high=depth.reduce((v,item)=>v===null||comparePrice(item[0],v)>0?item[0]:v,null);
      const inside=[];
      for(const order of orders){
        if(low===null||comparePrice(order.price,low)<0||comparePrice(order.price,high)>0)outside.push(order);else inside.push(order);
      }
      const prices=[...new Set([...depth.map(item=>item[0]),...inside.map(order=>order.price)])].sort((a,b)=>comparePrice(b,a));
      for(const price of prices){
        const own=inside.filter(order=>order.price===price).map(order=>`${order.filled===null?'확인 대기':order.remaining} (${states[order.status]})`).join(' + ');
        const tr=row([side(direction),price,depth.find(item=>item[0]===price)?.[1],own||'—']);
        tr.className='replay-'+direction;priceCell(tr,price);levels.push(tr);
      }
    }
    $('book').replaceChildren(...levels);
    $('outside').hidden=!outside.length;
    $('outside-rows').replaceChildren(...outside.map(order=>row([side(order.side),order.price,`${order.filled===null?'확인 대기':order.remaining} · ${states[order.status]}`])));
  }
  function render(){
    if(!snapshot)return;
    if(pending){
      const stored=snapshot.orders.find(order=>order.client_order_id===pending.id&&order.key_id===pending.keyId);
      if(stored){if(['sending','unknown'].includes(stored.status))pending.unresolved=true;else pending=null;}
    }
    renderKeys();renderBook();
    const market=snapshot.market;
    $('feed-status').textContent=`${exchange()} ${market.ticker||ticker()} · ${market.status} · 호가 경과 ${market.age_ms??'—'}ms · ${market.can_order?'신규 주문 가능 시세':'시세 수신 대기·지연: 신규 주문 차단'}`;
    $('ticks').replaceChildren(...market.ticks.slice(0,100).map(tick=>row([stamp(tick.time),side(tick.side),tick.price,tick.quantity])));
    $('tick-count').textContent=`최근 ${Math.min(100,market.ticks.length)}개`;
    $('orders').replaceChildren(...snapshot.orders.map(order=>{
      const tr=row([stamp(order.created_ms),`${order.exchange} ${order.ticker}`,side(order.side),order.price,order.quantity,order.filled,order.remaining,states[order.status]||order.status,stamp(order.last_checked_ms),'']);
      if(order.error)tr.title=order.error;
      const action=name=>command(async()=>{
        const result=await api(`/api/manual/orders/${order.key_id}/${order.client_order_id}/${name}`,{method:'POST',body:{}});
        $('order-status').textContent=`${result.client_order_id} · ${states[result.status]}${result.error?' · '+result.error:''}`;
      });
      const refresh=button('상태 조회',()=>action('refresh')),cancel=button('취소 요청',()=>action('cancel'));
      refresh.disabled=!allowDirectTrading;cancel.disabled=!allowDirectTrading||terminal.has(order.status);tr.lastChild.append(refresh,cancel);return tr;
    }));
    controls();
  }
  async function refresh(){
    if(loading)return;
    loading=true;const epoch=version,venue=exchange();
    try{const result=await api('/api/manual?'+new URLSearchParams({exchange:venue}));if(epoch===version&&venue===exchange()){snapshot=result;render();}}
    catch(e){if(epoch===version){error(e.message);snapshot=null;controls();}}
    finally{loading=false;}
  }
  function schedule(){clearInterval(timer);if(visible)timer=setInterval(()=>{if(!document.hidden)refresh();},1000);}
  $('connect-form').addEventListener('submit',event=>{
    event.preventDefault();$('ticker').value=ticker();
    command(()=>api('/api/manual/market',{method:'POST',body:{exchange:exchange(),ticker:ticker()}}));
  });
  $('exchange').addEventListener('change',()=>{version++;snapshot=null;$('confirm').checked=false;controls();refresh();});
  $('ticker').addEventListener('input',()=>{$('confirm').checked=false;controls();});
  $('key-id').addEventListener('change',()=>{$('confirm').checked=false;if(snapshot)renderBook();controls();});
  $('confirm').addEventListener('change',controls);
  for(const id of ['price','quantity','side'])$(id).addEventListener('input',()=>{$('confirm').checked=false;controls();});
  $('key-exchange').addEventListener('change',()=>{const okx=$('key-exchange').value==='okx';$('passphrase').disabled=!okx;$('passphrase').required=okx;if(!okx)$('passphrase').value='';});
  $('key-form').addEventListener('submit',event=>{
    event.preventDefault();
    command(async()=>{
      if(!allowDirectTrading)return;
      const body={exchange:$('key-exchange').value,label:$('key-label').value,api_key:$('api-key').value,secret:$('secret').value,passphrase:$('passphrase').value};
      try{await api('/api/manual/keys',{method:'POST',body});$('key-status').textContent='등록했습니다. 잔고 조회·연결 확인을 실행하세요.';}
      finally{for(const id of ['api-key','secret','passphrase'])$(id).value='';}
    });
  });
  $('order-form').addEventListener('submit',event=>{
    event.preventDefault();
    if($('submit').disabled)return;
    const body={key_id:$('key-id').value,exchange:exchange(),ticker:ticker(),side:$('side').value,price:$('price').value,quantity:$('quantity').value,confirm_live:$('confirm').checked};
    const signature=JSON.stringify(body);
    if(pending&&pending.signature!==signature){error('이전 주문의 응답을 먼저 상태 조회로 확인하세요. 확인 전에는 새 요청으로 바꿀 수 없습니다.');return;}
    if(!pending)pending={id:crypto.randomUUID().replaceAll('-',''),keyId:body.key_id,signature};
    body.client_order_id=pending.id;
    command(async()=>{
      try{
        const result=await api('/api/manual/orders',{method:'POST',body});
        $('order-status').textContent=`${result.client_order_id} · ${states[result.status]}${result.error?' · '+result.error:''}`;
        if(['unknown','sending'].includes(result.status))pending.unresolved=true;else pending=null;
      }catch(e){if(e.data?.order_not_submitted===true)pending=null;throw e;}finally{$('confirm').checked=false;}
    });
  });
  $('refresh').addEventListener('click',refresh);
  window.addEventListener('pagehide',()=>clearInterval(timer));
  return {ready:Promise.resolve(),onEnter(){visible=true;refresh();schedule();},onLeave(){visible=false;version++;clearInterval(timer);$('confirm').checked=false;controls();}};
}
