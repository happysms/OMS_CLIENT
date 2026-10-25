import {$, exchanges, number, time} from '../../shared/dom.js';

const ROOT='@root';
const PAGE_SIZE=100;
const kindLabels={ob:'호가',price:'호가',orderbook:'호가',ob_1s:'1초 호가',orderbook_1s:'1초 호가',tick:'체결 틱',trade:'체결 틱',funding:'펀딩비율',candle:'1분봉',candles_1m:'1분봉',candle_1m:'1분봉',fx:'환율'};
const size=value=>Number.isFinite(value)?value<1024?number(value,0)+' B':value<1048576?number(value/1024,1)+' KiB':value<1073741824?number(value/1048576,1)+' MiB':number(value/1073741824,2)+' GiB':'—';
const name=item=>String(item?.label||item?.name||'이름 없음');

export function initFiles({api,onOpenQuery}={}){
  const state={generation:0,records:new Map(),focusId:null,selectedId:null,selectionVersion:0,info:null,infoLoading:false,infoError:null,infoController:null,opening:false,openError:null,search:'',searchPending:false,searchTimer:null,collapsed:false,destroyed:false};
  const panel=$('file-explorer'),layout=$('app-layout'),body=$('files-body'),tree=$('files-tree'),search=$('files-search');
  const media=window.matchMedia?.('(max-width: 760px)');
  const mobile=()=>media?media.matches:window.innerWidth<=760;
  const rootRecord=()=>state.records.get(ROOT);
  const recordName=record=>record.id===ROOT?'데이터 파일':name(record.item);
  function isCurrent(record,generation,request){return !state.destroyed&&generation===state.generation&&state.records.get(record.id)===record&&request===record.request;}
  function makeRecord(item,parent=null,level=0){return {id:item?.id??ROOT,item,parent,level,expanded:level===0,children:[],loaded:false,loading:false,error:null,request:0,controller:null,nextOffset:null,total:0};}
  function abortRequests(){for(const record of state.records.values())record.controller?.abort();state.infoController?.abort();}
  function setCollapsed(collapsed){
    state.collapsed=collapsed;layout.classList.toggle('files-collapsed',collapsed);panel.classList.toggle('collapsed',collapsed);body.hidden=collapsed;
    $('files-toggle').setAttribute('aria-expanded',String(!collapsed));$('files-toggle').setAttribute('aria-label',collapsed?'데이터 파일 펼치기':'데이터 파일 접기');
    $('files-toggle').title=collapsed?'데이터 파일 펼치기':'데이터 파일 접기';$('files-toggle-icon').textContent=collapsed?'▸':'◂';
    if(collapsed&&body.contains(document.activeElement))$('files-toggle').focus();
  }
  function updateStatus(){
    const root=rootRecord();
    $('files-status').textContent=state.searchPending?'검색어를 적용하고 있습니다.':root?.loading?'목록을 불러오고 있습니다.':root?.error?'목록을 불러오지 못했습니다.':state.search?`종목 “${state.search}” · 폴더를 펼쳐 확인하세요.`:'폴더를 펼치면 하위 목록을 불러옵니다.';
    tree.setAttribute('aria-busy',String(!!root?.loading||state.searchPending));tree.classList.toggle('search-pending',state.searchPending);
  }
  function focusNode(id){
    const button=[...tree.querySelectorAll('[data-node-id]')].find(node=>node.dataset.nodeId===id);
    if(!button)return;state.focusId=id;for(const row of tree.querySelectorAll('[data-node-id]'))row.tabIndex=row===button?0:-1;button.focus();
  }
  function keydown(event,record){
    if(state.searchPending)return;
    const rows=[...tree.querySelectorAll('[data-node-id]')],position=rows.findIndex(row=>row.dataset.nodeId===record.id);
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();const next=rows[position+(event.key==='ArrowDown'?1:-1)];if(next)focusNode(next.dataset.nodeId);}
    else if(event.key==='Home'||event.key==='End'){event.preventDefault();const next=event.key==='Home'?rows[0]:rows.at(-1);if(next)focusNode(next.dataset.nodeId);}
    else if(event.key==='ArrowRight'&&record.item.kind==='directory'&&record.item.expandable!==false){event.preventDefault();if(!record.expanded)toggleDirectory(record,true);else if(record.children.length)focusNode(record.children[0]);}
    else if(event.key==='ArrowLeft'){event.preventDefault();if(record.expanded&&record.item.kind==='directory')toggleDirectory(record,false);else if(record.parent!==ROOT)focusNode(record.parent);}
  }
  function renderTree(){
    const active=document.activeElement,hadFocus=tree.contains(active),previousFocus=active?.dataset?.nodeId,previousAction=active?.dataset?.listAction,previousOwner=active?.dataset?.owner;
    tree.replaceChildren();let groupNumber=0;
    function renderChildren(record,group){
      for(const id of record.children){
        const child=state.records.get(id);if(!child)continue;
        const li=document.createElement('li');li.setAttribute('role','none');
        const button=document.createElement('button');button.type='button';button.className='files-node '+child.item.kind;button.dataset.nodeId=id;button.setAttribute('role','treeitem');button.setAttribute('aria-level',String(child.level));button.setAttribute('aria-selected',String(id===state.selectedId));button.tabIndex=id===state.focusId?0:-1;
        if(child.item.kind==='directory'&&child.item.expandable!==false){button.setAttribute('aria-expanded',String(child.expanded));button.setAttribute('aria-busy',String(child.loading));}
        button.classList.toggle('selected',id===state.selectedId);button.title=name(child.item);
        const arrow=document.createElement('span');arrow.className='files-node-arrow';arrow.setAttribute('aria-hidden','true');arrow.textContent=child.item.kind==='directory'?(child.item.expandable===false?'·':child.expanded?'▾':'▸'):'·';
        const icon=document.createElement('span');icon.className='files-node-icon';icon.setAttribute('aria-hidden','true');icon.textContent=child.item.kind==='directory'?'▱':'▤';
        const label=document.createElement('span');label.className='files-node-name';label.textContent=name(child.item);button.append(arrow,icon,label);
        button.addEventListener('focus',()=>{state.focusId=id;for(const row of tree.querySelectorAll('[data-node-id]'))row.tabIndex=row===button?0:-1;});
        button.addEventListener('keydown',event=>keydown(event,child));
        button.addEventListener('click',()=>{if(state.searchPending)return;state.focusId=id;if(child.item.kind==='directory')toggleDirectory(child);else selectFile(child);});
        li.append(button);group.append(li);
        if(child.expanded&&child.item.kind==='directory'){const children=document.createElement('ul');children.setAttribute('role','group');children.id='files-group-'+(++groupNumber);button.setAttribute('aria-controls',children.id);button.setAttribute('aria-owns',children.id);li.append(children);renderChildren(child,children);}
      }
      if(record.loading||record.error||(!record.children.length&&record.loaded)){
        const li=document.createElement('li');li.setAttribute('role','none');li.className='files-list-message';
        if(record.error){
          const message=document.createElement('span');message.setAttribute('role','alert');message.textContent=record.error;li.append(message);
          const retry=document.createElement('button');retry.type='button';retry.className='small-button';retry.dataset.listAction='retry';retry.dataset.owner=record.id;retry.textContent='다시 시도';retry.setAttribute('aria-label',recordName(record)+' 다시 불러오기');retry.addEventListener('click',()=>loadChildren(record,{append:record.loaded}));li.append(retry);
        }else{li.textContent=record.loading?'불러오는 중…':state.search?'일치하는 항목이 없습니다.':'저장된 항목이 없습니다.';if(record.loading)li.setAttribute('role','status');}
        group.append(li);
      }
      if(record.nextOffset!==null&&!record.loading&&!record.error){
        const li=document.createElement('li');li.setAttribute('role','none');const more=document.createElement('button');more.type='button';more.className='files-more';more.dataset.listAction='more';more.dataset.owner=record.id;
        more.textContent=`더 보기 · ${number(record.children.length,0)} / ${number(record.total,0)}`;more.setAttribute('aria-label',recordName(record)+' 더 보기');more.addEventListener('click',()=>loadChildren(record,{append:true}));li.append(more);group.append(li);
      }
    }
    const root=rootRecord();if(root)renderChildren(root,tree);
    const rows=[...tree.querySelectorAll('[data-node-id]')];if(!rows.some(row=>row.dataset.nodeId===state.focusId)){state.focusId=rows[0]?.dataset.nodeId??null;if(rows[0])rows[0].tabIndex=0;}
    if(hadFocus){const action=previousAction?[...tree.querySelectorAll('[data-list-action]')].find(node=>node.dataset.listAction===previousAction&&node.dataset.owner===previousOwner):null;if(action)action.focus();else focusNode(previousFocus&&rows.some(row=>row.dataset.nodeId===previousFocus)?previousFocus:state.focusId);}
    updateStatus();
  }
  async function loadChildren(record,{append=false}={}){
    if(state.destroyed||record.loading)return;
    const generation=state.generation,request=++record.request,offset=append?record.nextOffset??0:0;
    record.controller?.abort();record.controller=new AbortController();record.loading=true;record.error=null;renderTree();
    const parameters=new URLSearchParams({offset:String(offset),limit:String(PAGE_SIZE)});if(record.id!==ROOT)parameters.set('node',record.id);if(state.search)parameters.set('q',state.search);
    try{
      const result=await api('/api/data/files?'+parameters,{signal:record.controller.signal});
      if(!isCurrent(record,generation,request))return;
      if(!Array.isArray(result.items)||result.items.some(item=>typeof item?.id!=='string'||!['directory','file'].includes(item.kind)))throw new Error('목록의 응답 형식을 확인하지 못했습니다. 다시 불러오세요.');
      if(result.has_more&&(!Number.isInteger(result.next_offset)||result.next_offset<=offset))throw new Error('다음 목록 위치를 확인하지 못했습니다. 다시 불러오세요.');
      if(!append)record.children=[];
      const added=[];
      for(const item of result.items){
        if(item.id===ROOT)continue;
        let child=state.records.get(item.id);
        if(!child||child.parent!==record.id){child=makeRecord(item,record.id,record.level+1);state.records.set(item.id,child);}else child.item=item;
        if(!record.children.includes(item.id))record.children.push(item.id);
        if(state.preserveOpen?.has(item.id)&&child.item.kind==='directory'&&child.item.expandable!==false){
          state.preserveOpen.delete(item.id);
          if(record.expanded){child.expanded=true;if(!child.loaded&&!child.loading)added.push(child);}
        }
      }
      record.loaded=true;record.nextOffset=result.has_more?result.next_offset:null;record.total=Number.isInteger(result.total)?result.total:record.children.length;record.loading=false;record.error=null;renderTree();
      // Refresh only the exchange/category branches the user had already opened.
      await Promise.allSettled(added.map(child=>loadChildren(child)));
    }catch(error){if(isCurrent(record,generation,request)&&error.name!=='AbortError'){record.loading=false;record.error=error.message||'목록을 불러오지 못했습니다.';renderTree();}}
  }
  function toggleDirectory(record,expanded=!record.expanded){
    if(record.item.expandable===false)return;
    record.expanded=expanded;if(!expanded&&record.children.includes(state.focusId))state.focusId=record.id;renderTree();if(expanded&&!record.loaded&&!record.loading)loadChildren(record);
  }
  function clearInfo(){
    state.selectionVersion++;state.infoController?.abort();state.selectedId=null;state.info=null;state.infoLoading=false;state.infoError=null;state.openError=null;state.opening=false;renderInfo();
  }
  function renderInfo(){
    const target=$('files-info');target.replaceChildren();target.hidden=!state.selectedId;
    if(!state.selectedId)return;
    const heading=document.createElement('h3');heading.textContent=name(state.records.get(state.selectedId)?.item);target.append(heading);
    if(state.infoLoading){const loading=document.createElement('p');loading.className='files-info-message';loading.setAttribute('role','status');loading.textContent='파일 정보를 확인하고 있습니다.';target.append(loading);return;}
    if(state.infoError){
      const error=document.createElement('p');error.className='files-info-message error';error.setAttribute('role','alert');error.textContent=state.infoError;const retry=document.createElement('button');retry.type='button';retry.className='small-button';retry.textContent='파일 정보 다시 시도';retry.addEventListener('click',()=>{const record=state.records.get(state.selectedId);if(record)selectFile(record);});target.append(error,retry);return;
    }
    const info=state.info;if(!info)return;
    const kind=kindLabels[info.data_kind]||'시장 데이터',identity=document.createElement('p');identity.className='files-info-identity';identity.textContent=[exchanges[info.exchange]||info.exchange,kind,info.ticker].filter(Boolean).join(' · ');target.append(identity);
    const path=document.createElement('p');path.className='files-info-path';path.textContent=info.relative_path||info.name||'';target.append(path);
    const details=document.createElement('dl');details.className='files-metadata';
    for(const [label,value] of [['형식',info.format||'—'],['크기',size(info.bytes)],['행 수',Number.isFinite(info.rows)?number(info.rows,0):'—'],['수정 시각',info.modified_at?time(info.modified_at)+' KST':'—']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;details.append(dt,dd);}target.append(details);
    if(info.query){
      const range=document.createElement('p');range.className='files-info-message';range.textContent=`조회 구간 ${time(info.query.start)} ~ ${time(info.query.end)} KST`;target.append(range);
      const open=document.createElement('button');open.type='button';open.id='files-open-query';open.className='primary-button';open.disabled=state.opening||typeof onOpenQuery!=='function';open.textContent=state.opening?'구간을 열고 있습니다…':'이 파일 구간 조회';open.addEventListener('click',openQuery);target.append(open);
    }else{const note=document.createElement('p');note.className='files-info-message';note.textContent=info.message||'파일 정보를 확인할 수 있습니다. 이 형식의 구간 조회는 아직 지원하지 않습니다.';target.append(note);}
    if(info.query&&info.message){const note=document.createElement('p');note.className='files-info-message';note.textContent=info.message;target.append(note);}
    if(state.openError){const error=document.createElement('p');error.className='files-info-message error';error.setAttribute('role','alert');error.textContent=state.openError;target.append(error);}
    if(Array.isArray(info.columns)&&info.columns.length){
      const columns=document.createElement('details');columns.className='files-columns';const summary=document.createElement('summary');summary.textContent=`컬럼 ${number(info.columns.length,0)}개`;columns.append(summary);
      const list=document.createElement('dl');for(const column of info.columns){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=String(column.name??'');dd.textContent=String(column.type??'');list.append(dt,dd);}columns.append(list);target.append(columns);
    }
  }
  async function selectFile(record){
    const version=++state.selectionVersion,generation=state.generation;state.infoController?.abort();state.infoController=new AbortController();state.selectedId=record.id;state.info=null;state.infoLoading=true;state.infoError=null;state.openError=null;state.opening=false;renderTree();renderInfo();
    try{
      const result=await api('/api/data/files/info?'+new URLSearchParams({node:record.id}),{signal:state.infoController.signal});
      if(state.destroyed||version!==state.selectionVersion||generation!==state.generation||state.selectedId!==record.id)return;
      if(result?.id!==record.id)throw new Error('선택한 파일과 응답 정보가 다릅니다. 다시 선택해 주세요.');state.info=result;state.infoLoading=false;renderInfo();
    }catch(error){if(!state.destroyed&&version===state.selectionVersion&&generation===state.generation&&error.name!=='AbortError'){state.infoLoading=false;state.infoError=error.message||'파일 정보를 읽지 못했습니다.';renderInfo();}}
  }
  async function openQuery(){
    if(!state.info?.query||state.opening||typeof onOpenQuery!=='function')return;
    const version=state.selectionVersion;state.opening=true;state.openError=null;renderInfo();
    try{await onOpenQuery({...state.info.query});if(version===state.selectionVersion&&mobile())setCollapsed(true);}
    catch(error){if(version===state.selectionVersion)state.openError=error.message||'선택한 파일 구간을 열지 못했습니다. 다시 시도해 주세요.';}
    finally{if(version===state.selectionVersion){state.opening=false;renderInfo();}}
  }
  function refresh(){
    clearTimeout(state.searchTimer);state.searchPending=false;state.generation++;abortRequests();clearInfo();
    state.preserveOpen=new Set([...state.records.values()].filter(record=>record.expanded&&record.level>0&&record.level<=2).map(record=>record.id));
    state.records.clear();const root=makeRecord(null);state.records.set(ROOT,root);state.focusId=null;renderTree();return loadChildren(root);
  }
  function searchChanged(){
    clearTimeout(state.searchTimer);state.search=search.value.trim();state.searchPending=true;state.generation++;abortRequests();clearInfo();updateStatus();
    state.searchTimer=setTimeout(refresh,250);
  }
  $('files-toggle').addEventListener('click',()=>setCollapsed(!state.collapsed));$('files-refresh').addEventListener('click',refresh);search.addEventListener('input',searchChanged);
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'&&!state.collapsed){event.preventDefault();setCollapsed(true);$('files-toggle').focus();}});
  const resized=()=>{if(mobile()&&!state.collapsed)setCollapsed(true);};if(media?.addEventListener)media.addEventListener('change',resized);else window.addEventListener('resize',resized);
  setCollapsed(mobile());const ready=refresh();
  return {ready,refresh,destroy(){state.destroyed=true;state.generation++;clearTimeout(state.searchTimer);abortRequests();if(media?.removeEventListener)media.removeEventListener('change',resized);else window.removeEventListener('resize',resized);}};
}
