export function initTabs({modules,stopLive}) {
  const state={module:'analysis'};
  function selectModule(module) {
    if(state.module===module)return;
    const previous=state.module;state.module=module;
    for(const tab of document.querySelectorAll('[data-module]')){const selected=tab.dataset.module===module;tab.classList.toggle('active',selected);tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
    for(const panel of document.querySelectorAll('[role="tabpanel"]'))panel.hidden=panel.id!=='module-'+module;
    if(previous==='analysis')stopLive({quiet:true}).catch(()=>{});
    modules[module]?.onEnter?.();
    try{history.replaceState(null,'','#'+module);}catch{/* Tabs remain usable in embedded contexts. */}
  }
  const tabs=[...document.querySelectorAll('[data-module]')];
  tabs.forEach((tab,index)=>{
    tab.addEventListener('click',()=>selectModule(tab.dataset.module));
    tab.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();tabs[next].focus();selectModule(tabs[next].dataset.module);});
  });
  const initial=location.hash?.slice(1);
  if(tabs.some(tab=>tab.dataset.module===initial))selectModule(initial);
  return {selectModule};
}
