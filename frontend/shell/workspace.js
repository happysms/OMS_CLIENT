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
  let message='실시간 수동매매를 사용하려면 진행 중인 시뮬레이션이 끝난 뒤 서버를 재시작하고 새로고침하세요.';
  if(session?.capabilities?.manual_spot_orders){
    try {
      const {initManualTrading}=await import('../features/manual_trading/controller.js');
      return initManualTrading({api});
    } catch {
      message='수동매매 화면을 불러오지 못했습니다. 서버 버전과 연결을 확인한 뒤 새로고침하세요.';
    }
  }
  const panel=document.getElementById('module-manual');
  for(const input of panel.querySelectorAll('input,select,button'))input.disabled=true;
  const error=document.getElementById('manual-error');error.hidden=false;error.textContent=message;
  return {ready:Promise.resolve()};
}

export async function initWorkspace({api,getCatalog,stopLive,session}) {
  const context={api,getCatalog};
  const modules={strategies:initStrategies(context),replay:initReplay(context),data:initData(context),quality:initQuality(context),backtest:initBacktesting(context),paper:initPaperTrading(context),manual:await initManualFeature({api,session})};
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
