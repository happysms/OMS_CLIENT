const $=id=>document.getElementById('strategies-'+id);
export function initStrategies({api}){
  let templates=[],items=[],editing=null,loading=0,saving=false;
  const error=text=>{$('error').hidden=!text;$('error').textContent=text||'';};
  const template=()=>templates.find(t=>t.type===$('type').value);
  function timingPreview(){
    const isPoint=$('type').value==='point2';$('timing-preview').hidden=!isPoint;
    if(!isPoint)return;
    const values=['hedge_fill_notice_ms','hedge_decision_ms','hedge_submit_ms','hedge_ack_ms','hedge_timeout_ms'].map(key=>Number(document.getElementById('strategy-param-'+key)?.value));
    if(values.some(value=>!Number.isFinite(value))){$('timing-preview').textContent='헤지 지연 값을 입력하세요.';return;}
    const entry=values[0]+values[1]+values[2],ack=entry+values[3];
    $('timing-preview').textContent=`설정상 원체결 → 헤지 도착 ${entry}ms / 접수 응답 ${ack}ms / 완료 제한 ${values[4]}ms. 주문 차단·대기열·부분체결 시간은 별도입니다. Point 실행 연결 전이며 실제 체결시간을 뜻하지 않습니다.`;
  }
  $('parameters').addEventListener('input',timingPreview);
  function parameters(values={}){
    const t=template();$('type-help').textContent=t?`${t.description} ${t.execution_note}`:'';
    $('parameters').replaceChildren(...(t?.fields||[]).map(field=>{
      const div=document.createElement('div');div.className='field';
      const label=document.createElement('label');label.htmlFor='strategy-param-'+field.key;label.textContent=field.label;
      const input=document.createElement(field.kind==='choice'?'select':'input');input.id=label.htmlFor;input.dataset.key=field.key;input.required=true;
      if(field.kind==='choice'){for(const [value,text] of Object.entries(field.options))input.add(new Option(text,value));}
      else{input.type='number';input.min=field.min;input.max=field.max;input.step=field.kind==='integer'?'1':'any';}
      input.value=values[field.key]??field.default;div.append(label,input);return div;
    }));
    timingPreview();
  }
  function edit(item=null){
    editing=item;error('');$('type').disabled=Boolean(item);$('name').value=item?.name||'';$('description').value=item?.description||'';
    if(item)$('type').value=item.type;
    parameters(item?.parameters);$('copy').disabled=!item;$('save').disabled=Boolean(item?.archived);
    $('save').textContent=item?'새 버전 저장':'전략 등록';
    $('message').textContent=item?`${item.name} · v${item.revision} 편집. 저장하면 이전 버전을 유지하고 새 버전을 만듭니다.`:'새 전략을 등록합니다. 종목·초기잔고·기간은 계좌 생성 시 지정합니다.';
  }
  function button(text,action){const node=document.createElement('button');node.type='button';node.className='small-button';node.textContent=text;node.addEventListener('click',action);return node;}
  function render(){
    $('list').replaceChildren(...items.map(item=>{
      const tr=document.createElement('tr');
      for(const value of [item.name,templates.find(t=>t.type===item.type)?.label||item.type,'v'+item.revision,item.archived?'보관됨':item.executable?'실행 가능':'등록됨 · 실행 연결 필요']){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td');td.className='table-pages';
      td.append(button('편집',()=>edit(item)),button('모의계좌에 적용',()=>{
        document.dispatchEvent(new CustomEvent('oms:select-strategy',{detail:item}));document.getElementById('tab-replay').click();
      }),button(item.archived?'복원':'보관',async()=>{
        try{await api(`/api/strategies/${item.id}/archive`,{method:'POST',body:{archived:!item.archived}});await refresh();if(editing?.id===item.id)edit(items.find(i=>i.id===item.id));}catch(e){error(e.message);}
      }));
      td.children[1].disabled=item.archived;tr.append(td);return tr;
    }));
  }
  async function refresh(){
    const generation=++loading;
    const response=await api('/api/strategies');if(generation!==loading)return;
    const previous=$('type').value;templates=response.templates;items=response.items;
    $('type').replaceChildren(...templates.map(t=>new Option(t.label,t.type)));if(templates.some(t=>t.type===previous))$('type').value=previous;
    if(!$('parameters').children.length)parameters();render();
    document.dispatchEvent(new CustomEvent('oms:strategies',{detail:items}));
  }
  $('type').addEventListener('change',()=>parameters());$('new').addEventListener('click',()=>{edit();$('name').focus();});
  $('copy').addEventListener('click',()=>{editing=null;$('type').disabled=false;$('name').value=($('name').value+' 복사').slice(0,80);$('save').textContent='전략 등록';$('save').disabled=false;$('copy').disabled=true;$('message').textContent='별도 전략으로 등록할 설정을 준비했습니다.';});
  $('refresh').addEventListener('click',()=>refresh().catch(e=>error(e.message)));
  $('form').addEventListener('submit',async event=>{
    event.preventDefault();if(saving)return;const t=template();if(!t)return;
    const payload={name:$('name').value,type:t.type,description:$('description').value,parameters:{}};
    for(const spec of t.fields){const value=document.getElementById('strategy-param-'+spec.key).value;payload.parameters[spec.key]=spec.kind==='integer'?Number(value):value;}
    const selected=editing;
    saving=true;$('save').disabled=true;error('');
    try{
      if(selected)payload.expected_revision=selected.revision;
      const result=await api(selected?`/api/strategies/${selected.id}/revisions`:'/api/strategies',{method:'POST',body:payload});
      await refresh();edit(result);$('message').textContent=`${result.name} · v${result.revision} 저장 완료. ${result.execution_note}`;
    }catch(e){error(e.message);}finally{saving=false;$('save').disabled=Boolean(editing?.archived);}
  });
  return {ready:Promise.resolve(),onEnter(){refresh().catch(e=>error(e.message));}};
}
