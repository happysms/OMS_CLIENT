import {initData} from '../features/data/controller.js';
import {initBacktesting} from '../features/backtesting/controller.js';
import {initPaperTrading} from '../features/paper_trading/controller.js';
import {initStrategies} from '../features/strategies/controller.js';
import {initReplay} from '../features/replay/controller.js';
import {initFiles,initStorage} from '../features/files/controller.js';
import {initQuality} from '../features/quality/controller.js';
import {renderCatalogSummary} from '../shared/source-controls.js';
import {initTabs} from './tabs.js';

export async function initWorkspace({api,getCatalog,stopLive}) {
  const context={api,getCatalog};
  const modules={strategies:initStrategies(context),replay:initReplay(context),data:initData(context),quality:initQuality(context),backtest:initBacktesting(context),paper:initPaperTrading(context)};
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
