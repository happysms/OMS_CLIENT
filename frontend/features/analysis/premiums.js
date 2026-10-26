import {TimeChart} from '../../shared/charts.js';

export function initPremiums({api}){
  const $=id=>document.getElementById(id),details=$('premium-details');
  let result=null,selected=null,timer=null,loading=false,hourVersion=0;
  const number=(value,digits=4)=>Number.isFinite(value)?new Intl.NumberFormat('ko-KR',{maximumFractionDigits:digits}).format(value):'—';
  const time=value=>Number.isFinite(value)?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(value):'—';
  const error=value=>{$('premium-error').textContent=value?.message||String(value);$('premium-error').hidden=false;};
  const clearError=()=>{$('premium-error').hidden=true;};
  const chart=new TimeChart($('premium-hourly-chart'),{format:v=>number(v)+'%',onHover:t=>chart.setCursor(t),onZoom:range=>chart.setZoom(range)});
  function row(values){const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}return tr;}
  function render(){
    if(!result)return;
    const job=result.job||{},states={idle:'대기',running:'집계 중',complete:'집계 완료',partial:'일부 오류',failed:'집계 실패',interrupted:'중단 후 재개 대기'};
    $('premium-enabled').checked=result.enabled;$('premium-enabled').disabled=false;
    $('premium-status').textContent=`공통 ${result.items.length}종목 · 기준 ${time(result.end_ms)} KST (직전 시간까지) · ${states[job.state]||job.state}${job.state==='running'?` ${job.completed_units||0}/${job.total_units||0} · ${job.ticker||''}`:''} · ${result.enabled?`다음 확인 ${time(result.next_update_ms)} KST`:'자동 누적 꺼짐'}`;
    if(job.last_error||job.error)error(job.last_error||job.error);
    $('premium-refresh').disabled=job.state==='running';
    $('premium-rows').replaceChildren();
    const query=$('premium-search').value.trim().toUpperCase();
    for(const item of result.items.filter(item=>item.ticker.includes(query))){
      const a=item.windows['3'],b=item.windows['7'];
      const tr=row([item.ticker,number(a.mean_pct),number(a.coverage_pct,2)+'%',number(b.mean_pct),number(b.coverage_pct,2)+'%',time(item.latest_hour_ms),item.error_hours]);
      if(item.ticker===selected)tr.classList.add('premium-selected');
      const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='small-button';button.textContent='보기';button.setAttribute('aria-label',`${item.ticker} 시간별 괴리 보기`);button.addEventListener('click',()=>loadHourly(item.ticker).catch(error));td.append(button);tr.append(td);$('premium-rows').append(tr);
    }
    if(!$('premium-rows').children.length)$('premium-rows').append(row(['표시할 종목이 없습니다.']));
  }
  async function loadHourly(ticker){
    const version=++hourVersion,changed=selected!==ticker;selected=ticker;if(changed){chart.setZoom(null);chart.setData([],[]);$('premium-hourly-rows').replaceChildren();$('premium-hourly-title').textContent=`${ticker} · 시간별 괴리 불러오는 중`;}render();
    const data=await api(`/api/premiums/hourly?ticker=${encodeURIComponent(ticker)}`);
    if(version!==hourVersion)return;
    chart.setData(data.items.map(item=>item.hour_ms),[{label:`${ticker} 시간 평균`,values:data.items.map(item=>item.mean_pct),color:'#5bdfb7',isolatedPoints:true}],{gapMs:3600000});
    $('premium-hourly-title').textContent=`${ticker} · 최근 7일 시간별 괴리 평균`;
    $('premium-hourly-rows').replaceChildren();
    const labels={complete:'완료',partial:'일부 관측',empty:'관측 없음',pending:'집계 대기',error:'읽기 오류'};
    for(const item of [...data.items].reverse()){
      const tr=row([time(item.hour_ms),number(item.mean_pct),`${item.samples.toLocaleString()} / 3,600`,labels[item.status]||item.status]);
      if(item.error)tr.title=item.error;
      $('premium-hourly-rows').append(tr);
    }
  }
  function schedule(){clearTimeout(timer);if(details.open)timer=setTimeout(()=>{if(document.hidden||$('module-analysis').hidden){schedule();return;}load();},result?.job?.state==='running'?3000:15000);}
  async function load(){
    if(loading)return;loading=true;clearError();
    try{result=await api('/api/premiums');render();const ticker=result.items.some(item=>item.ticker===selected)?selected:result.items[0]?.ticker;if(ticker)await loadHourly(ticker);}
    catch(e){error(e);}finally{loading=false;schedule();}
  }
  details.addEventListener('toggle',()=>{if(details.open)load();else{clearTimeout(timer);chart.setCursor(null);}});
  $('premium-search').addEventListener('input',render);
  $('premium-reload').addEventListener('click',load);
  $('premium-refresh').addEventListener('click',async()=>{clearError();$('premium-refresh').disabled=true;try{await api('/api/premiums/refresh',{method:'POST',body:{}});$('premium-status').textContent='최근 7일의 새 데이터와 변경된 시간대를 확인하도록 요청했습니다.';clearTimeout(timer);timer=setTimeout(load,1000);}catch(e){error(e);$('premium-refresh').disabled=false;}});
  $('premium-enabled').addEventListener('change',async()=>{const enabled=$('premium-enabled').checked;$('premium-enabled').disabled=true;try{await api('/api/premiums/settings',{method:'POST',body:{enabled}});await load();}catch(e){$('premium-enabled').checked=!enabled;error(e);}finally{$('premium-enabled').disabled=false;}});
  $('premium-csv').addEventListener('click',async()=>{try{const blob=await api('/api/premiums/csv',{responseType:'blob'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='premium-averages.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error(e);}});
  window.addEventListener('pagehide',()=>clearTimeout(timer));
}
