import {TimeChart} from '../../shared/charts.js';
import {$, exchanges, milliseconds, number, time, local, parseLocal} from '../../shared/dom.js';

const COLORS=['#5bdfb7','#78b4e8','#f1b66e','#b8a1ed','#f08fa6','#92cce1','#c5cf87','#bda78c'];
const ms=value=>Number.isFinite(value)?number(value,3)+' ms':'—';
const identity=series=>(exchanges[series.exchange]||series.exchange)+' · '+(series.ticker||'종목 미확인');
const finiteTime=value=>Number.isFinite(value)?time(value)+' KST':'—';
const statusLabel=series=>series.status==='ok'?'시각 쌍 확인':series.status==='error'?'조회 오류':'자료 없음';

// Keep isolated observations visible while leaving missing intervals unconnected.
class TimestampChart extends TimeChart{
  draw(){
    super.draw();const ctx=this.ctx,rect=this.canvas.getBoundingClientRect();if(!ctx||rect.width<1||rect.height<1||!this.times.length)return;
    const [start,end]=this.domain(),indexes=this.times.map((t,i)=>t>=start&&t<=end?i:-1).filter(i=>i>=0);let low=Infinity,high=-Infinity;
    for(const line of this.series)for(const i of indexes)if(Number.isFinite(line.values[i])){low=Math.min(low,line.values[i]);high=Math.max(high,line.values[i]);}if(!Number.isFinite(low))return;
    const pad=(high-low)*.14||Math.abs(high)*.0005||.01;low-=pad;high+=pad;const {left,right}=this.plotArea(rect.width),bottom=rect.height-29;
    ctx.save();ctx.beginPath();ctx.rect(left,18,right-left,bottom-18);ctx.clip();
    for(const line of this.series){ctx.fillStyle=line.color;for(let position=0;position<indexes.length;position++){
      const i=indexes[position],value=line.values[i];if(!Number.isFinite(value))continue;
      const before=indexes[position-1],after=indexes[position+1];if(Number.isFinite(line.values[before])&&this.times[i]-this.times[before]<=this.gapMs*1.6||Number.isFinite(line.values[after])&&this.times[after]-this.times[i]<=this.gapMs*1.6)continue;
      const x=left+(this.times[i]-start)/Math.max(1,end-start)*(right-left),y=bottom-(value-low)/(high-low)*(bottom-18);ctx.beginPath();ctx.arc(x,y,2.3,0,Math.PI*2);ctx.fill();
    }}ctx.restore();
  }
}

export function initQuality({api,getCatalog}={}){
  const state={visible:false,catalog:null,catalogVersion:0,catalogController:null,catalogLoading:false,catalogPromise:null,version:0,controller:null,timer:null,running:false,result:null,buffers:new Map(),cards:new Map()};
  const compare=new TimestampChart($('quality-chart'),{format:ms,onHover:t=>compare.setCursor(t),onZoom:r=>compare.setZoom(r)});
  const checked=()=>[...$('quality-exchanges').querySelectorAll('input:checked')].map(input=>input.value);
  const live=()=>$('quality-mode').value==='live';
  const supportsLive=()=>state.catalog?.kinds?.find(kind=>kind.id===$('quality-kind').value)?.live_supported===true;
  const selectionKey=()=>JSON.stringify({kind:$('quality-kind').value,exchanges:checked(),ticker:$('quality-ticker').value.trim(),threshold:$('quality-threshold').value,start:$('quality-start').value,end:$('quality-end').value,interval:$('quality-interval').value,mode:$('quality-mode').value});
  function error(message){$('quality-error').textContent=message||'';$('quality-error').hidden=!message;}
  function resetButton(){$('quality-run').disabled=state.catalogLoading||!state.catalog;$('quality-run').textContent=live()?'현재 모니터 시작':'기록 시각 차이 조회';$('quality-stop').hidden=!state.running;}
  function stop(message){
    state.version++;state.controller?.abort();clearTimeout(state.timer);state.timer=null;state.running=false;resetButton();if(message)$('quality-message').textContent=message;
  }
  function clear(message='조건을 선택한 뒤 조회하세요.'){
    state.result=null;state.buffers.clear();for(const chart of state.cards.values())chart.destroy();state.cards.clear();$('quality-series').replaceChildren();
    compare.setData([],[]);compare.setZoom(null);compare.setCursor(null);$('quality-legend').replaceChildren();$('quality-placeholder').hidden=false;$('quality-placeholder').textContent=message;
    $('quality-warnings').hidden=true;$('quality-warnings').textContent='';$('quality-asof').textContent='아직 조회하지 않았습니다.';
  }
  function modeHelp(){
    const supported=supportsLive();$('quality-mode').querySelector('[value="live"]').disabled=!!state.catalog&&!supported;if(live()&&!supported&&state.catalog)$('quality-mode').value='history';
    $('quality-history-fields').hidden=live();$('quality-start').required=!live();$('quality-end').required=!live();
    $('quality-definition').textContent=$('quality-kind').value==='ob_1s'
      ?'USDT 1초 자료는 실제 채택 시각 − 거래소 호가 시각입니다. 호가의 나이가 포함되며 Parquet는 약 5초마다 갱신됩니다. 네트워크 왕복시간(RTT)이 아닙니다.'
      :'기록 시각 차이 = desktopTime − serverTime입니다. desktopTime은 상위 수집 PC의 기록 시각이며 시계 차이·전송·처리 시간이 함께 포함됩니다. 네트워크 왕복시간(RTT)이 아닙니다.';
    $('quality-live-help').hidden=!live();resetButton();
  }
  function changed(){stop();error('');clear('설정이 바뀌었습니다. 새 조건으로 조회하세요.');modeHelp();$('quality-message').textContent='설정이 바뀌었습니다. 조회를 다시 실행하세요.';}
  function populateExchanges(){
    const previous=new Set(checked()),available=Object.keys(state.catalog?.exchanges?.[$('quality-kind').value]||{}),preserve=available.some(exchange=>previous.has(exchange));$('quality-exchanges').replaceChildren();
    available.forEach((exchange,index)=>{const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=exchange;input.checked=preserve?previous.has(exchange):index<8;
      input.addEventListener('change',()=>{if(checked().length>8){input.checked=false;error('한 번에 최대 8개 거래소를 선택할 수 있습니다.');return;}exchangeState();changed();});label.append(input,document.createTextNode(exchanges[exchange]||exchange));$('quality-exchanges').append(label);});
    if(checked().length>8)for(const input of [...$('quality-exchanges').querySelectorAll('input:checked')].slice(8))input.checked=false;
    exchangeState();
  }
  function exchangeState(){
    const inputs=[...$('quality-exchanges').querySelectorAll('input')],selected=checked();$('quality-all').checked=inputs.length>0&&selected.length===Math.min(inputs.length,8);$('quality-all').indeterminate=selected.length>0&&!$('quality-all').checked;$('quality-exchange-count').textContent=`${selected.length} / 최대 8개`;
    const symbols=new Set();for(const exchange of selected)for(const ticker of state.catalog?.exchanges?.[$('quality-kind').value]?.[exchange]||[])symbols.add(ticker);
    $('quality-tickers').replaceChildren(...[...symbols].sort().map(ticker=>new Option(ticker,ticker)));
  }
  async function loadCatalog(){
    if(state.catalogLoading)return state.catalogPromise;
    const version=++state.catalogVersion;state.catalogController?.abort();state.catalogController=new AbortController();state.catalogLoading=true;resetButton();$('quality-catalog-retry').hidden=true;
    state.catalogPromise=(async()=>{try{
      const catalog=await api('/api/quality/catalog',{signal:state.catalogController.signal});if(version!==state.catalogVersion||!state.visible)return;
      if(!Array.isArray(catalog.kinds)||!catalog.exchanges)throw new Error('데이터 품질 목록을 확인하지 못했습니다.');
      state.catalog=catalog;const previous=$('quality-kind').value;$('quality-kind').replaceChildren(...catalog.kinds.map(kind=>new Option(kind.label||kind.id,kind.id)));
      if(catalog.kinds.some(kind=>kind.id===previous))$('quality-kind').value=previous;else if(catalog.kinds.some(kind=>kind.id==='ob'))$('quality-kind').value='ob';
      populateExchanges();modeHelp();error('');$('quality-message').textContent='거래소·종목과 기간을 선택하세요. 종목을 비우면 각 거래소에서 BTC를 우선 선택합니다.';
    }catch(failure){if(version===state.catalogVersion&&failure.name!=='AbortError'){error(failure.message||'목록을 불러오지 못했습니다.');$('quality-catalog-retry').hidden=false;}}
    finally{if(version===state.catalogVersion){state.catalogLoading=false;resetButton();}}})();return state.catalogPromise;
  }
  function config(){
    const threshold=Number($('quality-threshold').value),selected=checked();
    if(!selected.length||selected.length>8)throw new Error('거래소를 1~8개 선택하세요.');if(!$('quality-threshold').value.trim()||!Number.isFinite(threshold)||threshold<0||threshold>86400000)throw new Error('임계값은 0~86,400,000 밀리초로 입력하세요.');
    const common={data_kind:$('quality-kind').value,exchanges:selected,ticker:$('quality-ticker').value.trim(),threshold_ms:threshold};
    if(live()){if(!supportsLive())throw new Error('선택한 데이터 종류는 현재 모니터를 지원하지 않습니다.');return common;}
    const start=$('quality-start').value,end=$('quality-end').value;if(!Number.isFinite(parseLocal(start))||!Number.isFinite(parseLocal(end))||parseLocal(start)>=parseLocal(end))throw new Error('시작보다 늦은 종료 시간을 입력하세요.');
    return {...common,start:new Date(parseLocal(start)).toISOString(),end:new Date(parseLocal(end)).toISOString(),interval:$('quality-interval').value,max_points:2000};
  }
  function current(version,key){return state.visible&&version===state.version&&key===selectionKey();}
  function warning(result){const warnings=Array.isArray(result.meta?.warnings)?result.meta.warnings:[];$('quality-warnings').textContent=warnings.join(' ');$('quality-warnings').hidden=!warnings.length;}
  function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
  function chartCard(series,index){
    const article=element('article','panel quality-source');article.dataset.exchange=series.exchange;
    const heading=element('div','panel-heading'),title=element('div');title.append(element('h2','',identity(series)));const status=element('span','quality-source-status',statusLabel(series));status.classList.toggle('bad',series.status!=='ok');heading.append(title,status);article.append(heading);
    const message=element('p','quality-source-message',series.error||'');if(series.error)message.setAttribute('role','status');article.append(message);
    const chartWrap=element('div','chart-wrap quality-source-chart'),canvas=document.createElement('canvas');canvas.id='quality-source-'+index;canvas.setAttribute('role','img');canvas.setAttribute('aria-label',identity(series)+' 기록 시각 차이 차트');chartWrap.append(canvas);article.append(chartWrap);
    const stats=element('dl','quality-source-stats');article.append(stats);$('quality-series').append(article);
    const chart=new TimestampChart(canvas,{format:ms,onHover:t=>chart.setCursor(t),onZoom:r=>chart.setZoom(r)});state.cards.set(series.exchange,chart);return {article,stats,chart};
  }
  function stat(list,label,value,{bad=false}={}){const pair=element('div',bad?'bad':''),dt=element('dt','',label),dd=element('dd','',value);pair.append(dt,dd);list.append(pair);}
  function cardLegend(article,lines){const legend=element('div','chart-footer quality-legend');for(const line of lines){const label=element('span','',line.label);label.style.setProperty('--quality-color',line.color);legend.append(label);}article.querySelector('.quality-source-chart').after(legend);}
  function comparison(series,valueField,timesField='times'){
    const times=[...new Set(series.flatMap(item=>item[timesField]||[]).filter(Number.isFinite))].sort((a,b)=>a-b);
    const lines=series.map((item,index)=>{const points=new Map((item[timesField]||[]).map((t,i)=>[t,item[valueField]?.[i]]));return {label:identity(item),values:times.map(t=>Number.isFinite(points.get(t))?points.get(t):null),color:COLORS[index%COLORS.length]};});
    compare.setData(times,lines,{gapMs:live()?2000:milliseconds[$('quality-interval').value]||60000});
    $('quality-placeholder').hidden=lines.some(line=>line.values.some(Number.isFinite));$('quality-placeholder').textContent='유효한 시각 쌍이 없습니다. 거래소별 사유를 확인하세요.';
    $('quality-legend').replaceChildren(...lines.map(line=>{const label=element('span','',line.label);label.style.setProperty('--quality-color',line.color);return label;}));
  }
  function resetCards(){for(const chart of state.cards.values())chart.destroy();state.cards.clear();$('quality-series').replaceChildren();}
  function renderHistory(result){
    resetCards();const series=result.series||[];comparison(series.map(item=>item.status==='ok'?item:{...item,times:[],mean_ms:[]}),'mean_ms');
    series.forEach((item,index)=>{
      const {article,stats,chart}=chartCard(item,index),s=item.stats||{},valid=item.status==='ok'&&s.valid_pairs!==0,latency=value=>valid?ms(value):'—',lines=[['mean_ms','평균','#5bdfb7'],['p95_ms','P95','#78b4e8'],['max_ms','최댓값','#f1b66e'],['min_ms','최솟값','#b8a1ed']].map(([key,label,color])=>({label,values:item[key]||[],color}));
      if(Array.isArray(item.received_mean_ms))lines.push({label:'수신−서버 평균',values:item.received_mean_ms,color:'#ed8fc6',dashed:true});chart.setData(valid?item.times||[]:[],valid?lines:[],{gapMs:milliseconds[$('quality-interval').value]||60000});cardLegend(article,lines);
      for(const [label,value] of [['평균',latency(s.mean_ms)],['P50 / P95',latency(s.p50_ms)+' / '+latency(s.p95_ms)],['최솟값 / 최댓값',latency(s.min_ms)+' / '+latency(s.max_ms)],['최근 유효 차이',latency(s.latest_ms)],['전체 행 / 유효 시각 쌍',number(s.rows,0)+' / '+number(s.valid_pairs,0)],['서버 시각 누락·무효',number(s.invalid_server_rows,0)],['기록 시각 무효',number(s.unplaced_desktop_rows,0)],['최근 기록 시각',finiteTime(s.latest_desktop_ms)],['해당 서버 시각',finiteTime(s.latest_server_ms)]])stat(stats,label,value);
      stat(stats,'음수',number(s.negative_count,0),{bad:s.negative_count>0});stat(stats,'임계 초과',number(s.above_threshold_count,0),{bad:s.above_threshold_count>0});
    });
    $('quality-asof').textContent=`${result.meta?.start?time(result.meta.start):$('quality-start').value} ~ ${result.meta?.end?time(result.meta.end):$('quality-end').value} KST · 구간별 평균 비교 · 원본 전체 통계`;
    $('quality-message').textContent='조회 완료 · 평균·P95·최댓값·최솟값은 같은 시각 구간의 통계입니다. 누락 구간은 연결하거나 이전 값으로 채우지 않습니다.';warning(result);
  }
  function renderLive(result,requested){
    const response=new Map((result.series||[]).map(item=>[item.exchange,item]));
    const entries=requested.exchanges.map(exchange=>response.get(exchange)||{exchange,ticker:requested.ticker,status:'no_data',error:'이번 응답에 해당 거래소의 자료가 없습니다.'});
    for(const item of entries){
      let buffer=state.buffers.get(item.exchange);if(!buffer||buffer.ticker!==item.ticker){buffer={ticker:item.ticker,points:new Map()};state.buffers.set(item.exchange,buffer);}
      if(item.status==='ok'&&Number.isFinite(item.desktop_ms)&&Number.isFinite(item.latency_ms))buffer.points.set(item.desktop_ms,{latency:item.latency_ms,received:item.received_latency_ms});
      const times=[...buffer.points.keys()].sort((a,b)=>a-b),cutoff=(times.at(-1)??0)-1800000;
      for(const t of times)if(t<cutoff)buffer.points.delete(t);while(buffer.points.size>900)buffer.points.delete([...buffer.points.keys()].sort((a,b)=>a-b)[0]);
    }
    resetCards();const plotted=entries.map(item=>{const points=state.buffers.get(item.exchange).points,times=[...points.keys()].sort((a,b)=>a-b);return {...item,times,latencies:times.map(t=>points.get(t).latency),received:times.map(t=>points.get(t).received??null)};});comparison(plotted,'latencies');
    plotted.forEach((item,index)=>{
      const {article,stats,chart}=chartCard(item,index);article.classList.toggle('above',item.above_threshold===true);article.classList.toggle('negative',item.negative===true);
      const lines=[{label:'기록−서버',values:item.latencies,color:COLORS[index%COLORS.length]}];if(item.received.some(Number.isFinite))lines.push({label:'수신−서버',values:item.received,color:'#ed8fc6',dashed:true});chart.setData(item.times,lines,{gapMs:2000});cardLegend(article,lines);
      stat(stats,'이번 응답의 차이',item.status==='ok'?ms(item.latency_ms):'—',{bad:item.above_threshold===true||item.negative===true});
      stat(stats,'자료 경과 시간',item.future_desktop===true||item.age_ms<0?'미래 기록 · 시계 확인':Number.isFinite(item.age_ms)?number(item.age_ms/1000,1)+'초':'—',{bad:item.age_ms>10000||item.age_ms<0||item.future_desktop===true});
      stat(stats,'마지막 자료 시각',finiteTime(Number.isFinite(item.desktop_ms)?item.desktop_ms:item.times.at(-1)));stat(stats,'거래소 서버 시각',finiteTime(item.server_ms));
      stat(stats,'읽은 자료',item.source==='parquet'?'1초 수집 저장분 · 약 5초 갱신':item.source==='redis'?'상위 수집기의 최신 자료':'확인되지 않음');stat(stats,'차트 표본',number(item.times.length,0)+'개 · 최대 900개 / 30분');
      if(Number.isFinite(item.received_latency_ms))stat(stats,'수신−서버 차이',ms(item.received_latency_ms));
      if(item.age_ms>10000)article.querySelector('.quality-source-message').textContent=(item.error?item.error+' ':'')+'마지막 자료가 10초 이상 오래됐습니다. 실시간 수신 상태를 확인하세요.';
    });
    $('quality-asof').textContent='응답 확인 '+finiteTime(result.observed_at_ms)+' · 차트 가로축은 실제 원본 기록 시각';warning(result);
  }
  async function run(event){
    event?.preventDefault();let requested;try{requested=config();}catch(failure){error(failure.message);return;}
    stop();const version=state.version,key=selectionKey();clear();error('');state.controller=new AbortController();
    if(live()){
      state.running=true;resetButton();$('quality-run').disabled=true;$('quality-message').textContent='2초마다 서버 자료를 확인합니다. 원본 기록 시각이 같은 자료는 중복으로 추가하지 않습니다.';
      async function poll(){
        if(!current(version,key)||!state.running)return;
        try{const result=await api('/api/quality/live',{method:'POST',body:requested,signal:state.controller.signal});if(!current(version,key)||!state.running)return;if(result.mode!=='live'||!Array.isArray(result.series))throw new Error('현재 모니터 응답을 확인하지 못했습니다.');state.result=result;error('');renderLive(result,requested);}
        catch(failure){if(current(version,key)&&failure.name!=='AbortError'){error(failure.message||'현재 자료를 확인하지 못했습니다.');if(state.result)renderLive({...state.result,series:state.result.series.map(item=>({...item,status:'error',latency_ms:null,age_ms:null,error:'현재 응답을 받지 못했습니다.'}))},requested);$('quality-message').textContent='현재 자료를 받지 못했습니다. 2초 뒤 다시 확인합니다. 기존 차트는 이전 표본입니다.';}}
        finally{if(current(version,key)&&state.running)state.timer=setTimeout(poll,2000);}
      }
      return poll();
    }
    $('quality-run').disabled=true;$('quality-run').textContent='조회 중…';$('quality-message').textContent='서버에서 거래소별 원본 시각 쌍을 계산하고 있습니다.';
    try{const result=await api('/api/quality/history',{method:'POST',body:requested,signal:state.controller.signal});if(!current(version,key))return;if(result.mode!=='history'||!Array.isArray(result.series))throw new Error('과거 조회 응답을 확인하지 못했습니다.');state.result=result;renderHistory(result);}
    catch(failure){if(current(version,key)&&failure.name!=='AbortError'){error(failure.message||'시각 차이를 조회하지 못했습니다.');$('quality-message').textContent='조회하지 못했습니다. 기간과 소스를 확인한 뒤 다시 실행하세요.';}}
    finally{if(current(version,key))resetButton();}
  }
  $('quality-form').addEventListener('submit',run);$('quality-stop').addEventListener('click',()=>stop('현재 모니터를 중지했습니다. 차트는 마지막 표본을 표시합니다.'));
  $('quality-kind').addEventListener('change',()=>{populateExchanges();changed();});$('quality-all').addEventListener('change',()=>{[...$('quality-exchanges').querySelectorAll('input')].forEach((input,index)=>{input.checked=$('quality-all').checked&&index<8;});exchangeState();changed();});
  for(const id of ['quality-mode','quality-interval','quality-start','quality-end'])$(id).addEventListener('change',changed);
  for(const id of ['quality-ticker','quality-threshold'])$(id).addEventListener('input',changed);
  $('quality-catalog-retry').addEventListener('click',loadCatalog);
  const now=Date.now();$('quality-start').value=local(now-86400000);$('quality-end').value=local(now);modeHelp();
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&state.running)stop('화면이 숨겨져 현재 모니터를 중지했습니다. 다시 시작하려면 현재 모니터 시작을 누르세요.');});
  return {ready:Promise.resolve(),refresh(){if(!state.visible){state.catalog=null;return Promise.resolve();}stop();clear();return loadCatalog();},onEnter(){state.visible=true;if(!state.catalog)return loadCatalog();compare.draw();for(const chart of state.cards.values())chart.draw();},onLeave(){state.visible=false;state.catalogVersion++;state.catalogController?.abort();state.catalogLoading=false;stop(state.running?'다른 탭으로 이동해 현재 모니터를 중지했습니다.':undefined);}};
}
