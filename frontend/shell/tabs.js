export function initTabs({ modules, stopLive }) {
    const state = { module: 'analysis' };
    function selectModule(module) {
        const aliases = {
            replay: 'historical',
            manual: 'live',
            'paper-live': 'live',
            'paper-historical': 'historical',
        };
        if (aliases[module]) {
            modules.paper?.setMode?.(aliases[module]);
            module = 'paper';
        }
        if (state.module === module) return;
        const previous = state.module;
        state.module = module;
        for (const tab of document.querySelectorAll('[data-module]')) {
            const selected = tab.dataset.module === module;
            tab.classList.toggle('active', selected);
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
        }
        for (const panel of document.querySelectorAll('[role="tabpanel"]'))
            panel.hidden = panel.id !== 'module-' + module;
        if (previous === 'analysis') stopLive({ quiet: true }).catch(() => {});
        modules[previous]?.onLeave?.();
        modules[module]?.onEnter?.();
        try {
            history.replaceState(null, '', '#' + (modules[module]?.route?.() || module));
        } catch {
            /* Tabs remain usable in embedded contexts. */
        }
    }
    const tabs = [...document.querySelectorAll('[data-module]')];
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => selectModule(tab.dataset.module));
        tab.addEventListener('keydown', (event) => {
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = tabs.length - 1;
            else return;
            event.preventDefault();
            tabs[next].focus();
            selectModule(tabs[next].dataset.module);
        });
    });
    const initial = location.hash?.slice(1);
    if (
        ['replay', 'manual', 'paper-live', 'paper-historical'].includes(initial) ||
        tabs.some((tab) => tab.dataset.module === initial)
    )
        selectModule(initial);
    return { selectModule };
}
