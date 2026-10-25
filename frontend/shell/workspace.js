import {initData} from '../features/data/controller.js';
import {initFunding} from '../features/funding/controller.js';
import {initBacktesting} from '../features/backtesting/controller.js';
import {initPaperTrading} from '../features/paper_trading/controller.js';
import {initFiles} from '../features/files/controller.js';
import {renderCatalogSummary} from '../shared/source-controls.js';
import {initTabs} from './tabs.js';

export async function initWorkspace({api,getCatalog,stopLive}) {
  const context={api,getCatalog};
  const modules={data:initData(context),funding:initFunding(context),backtest:initBacktesting(context),paper:initPaperTrading(context)};
  document.addEventListener('oms:catalog',()=>{
    renderCatalogSummary(getCatalog());
    for(const module of Object.values(modules))module.refresh?.();
  });
  renderCatalogSummary(getCatalog());
  // Missing data for one instrument must not block other screens.
  await Promise.allSettled(Object.values(modules).map(module=>module.ready));
  const tabs=initTabs({modules,stopLive});
  const files=initFiles({api,async onOpenQuery(query){
    const target=query.data_type==='funding'?'funding':'data';
    tabs.selectModule(target);
    await modules[target].openQuery(query);
  }});
  document.addEventListener('oms:catalog',()=>{files.refresh?.();});
}
