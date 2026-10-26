import {initData} from '../features/data/controller.js';
import {initBacktesting} from '../features/backtesting/controller.js';
import {initPaperTrading} from '../features/paper_trading/controller.js';
import {initStrategies} from '../features/strategies/controller.js';
import {initReplay} from '../features/replay/controller.js';
import {initFiles,initStorage} from '../features/files/controller.js';
import {initQuality} from '../features/quality/controller.js';
import {renderCatalogSummary} from '../shared/source-controls.js';
import {initTabs} from './tabs.js';

export async function initManualFeature({api,session}) {
  let message='실시간 호가·틱을 사용하려면 진행 중인 모의투자가 끝난 뒤 서버를 재시작하고 새로고침하세요.';
  if(session?.capabilities?.manual_market_view||session?.capabilities?.manual_spot_orders){
    try {
      const {initManualTrading}=await import('../features/manual_trading/controller.js');
      return initManualTrading({api,allowDirectTrading:false});
    } catch {
      message='실시간 시세 화면을 불러오지 못했습니다. 서버 버전과 연결을 확인한 뒤 새로고침하세요.';
    }
  }
  const panel=document.getElementById('module-manual');
  for(const input of panel.querySelectorAll('input,select,button'))input.disabled=true;
  const error=document.getElementById('manual-error');error.hidden=false;error.textContent=message;
  return {ready:Promise.resolve()};
}

// One paper-trading workspace; data source selects the existing compatible engine.
export function initUnifiedPaper({replay,live,market}) {
  const selector=document.getElementById('paper-data-mode');
  let mode='historical';
  let visible=false;
  const active=()=>mode==='historical'?[replay]:[live,market];

  function render(){
    selector.value=mode;
    document.getElementById('module-replay').hidden=mode!=='historical';
    document.getElementById('paper-live').hidden=mode!=='live';
    document.getElementById('paper-mode-help').textContent=mode==='historical'
      ?'기간·전략·초기잔고를 정해 새 가상계좌에서 실행합니다. 지정가·MM·빠른 실행·1~32배속을 지원합니다.'
      :'현재 시세로 가상 시장가 주문을 연습합니다. 빗썸·업비트 가상계좌와 빗썸·OKX 공개 호가·틱을 볼 수 있습니다. 실시간 지정가·MM 연결은 아직 지원하지 않습니다.';
  }

  function setMode(next){
    if(!['historical','live'].includes(next)||next===mode)return;
    if(visible){
      for(const controller of active())controller.onLeave?.();
    }
    mode=next;
    render();
    if(visible){
      for(const controller of active())controller.onEnter?.();
      try{
        history.replaceState(null,'',mode==='live'?'#paper-live':'#paper');
      }catch{/* Embedded views can still switch modes without changing the URL. */}
    }
  }

  selector.addEventListener('change',()=>setMode(selector.value));
  document.addEventListener('oms:paper-mode',event=>setMode(event.detail));
  render();
  return {
    ready:Promise.allSettled([replay.ready,live.ready,market.ready]),
    setMode,
    route:()=>mode==='live'?'paper-live':'paper',
    refresh(){
      replay.refresh?.();
      live.refresh?.();
    },
    onEnter(){
      if(visible)return;
      visible=true;
      render();
      for(const controller of active())controller.onEnter?.();
    },
    onLeave(){
      if(!visible)return;
      visible=false;
      for(const controller of active())controller.onLeave?.();
    },
  };
}

export async function initWorkspace({api,getCatalog,stopLive,session}) {
  const context={api,getCatalog};
  const strategies=initStrategies(context);
  const paper=initUnifiedPaper({replay:initReplay(context),live:initPaperTrading(context),market:await initManualFeature({api,session})});
  const modules={strategies,paper,data:initData(context),quality:initQuality(context),backtest:initBacktesting(context)};
  document.addEventListener('oms:catalog',()=>{
    renderCatalogSummary(getCatalog());
    for(const module of Object.values(modules))module.refresh?.();
  });
  renderCatalogSummary(getCatalog());
  // Missing data for one instrument must not block other screens.
  await Promise.allSettled(Object.values(modules).map(module=>module.ready));
  const tabs=initTabs({modules,stopLive});
  if(location.hash==='#funding'){
    const type=document.getElementById('data-type');type.value='funding';type.dispatchEvent(new Event('change'));
    tabs.selectModule('data');
  }
  const files=initFiles({api,async onOpenQuery(query,options={}){
    if(options.signal?.aborted||options.isCurrent?.()===false)return;
    tabs.selectModule('data');
    await modules.data.openQuery(query,options);
  }});
  document.addEventListener('oms:catalog',()=>{files.refresh?.();});
  initStorage({api,onCleaned:()=>files.refresh()});
}
