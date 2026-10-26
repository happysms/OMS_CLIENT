import { createApi } from '../shared/api.js';
import { initAnalysis } from '../features/analysis/controller.js';
import { initPremiums } from '../features/analysis/premiums.js';
import { initWorkspace } from './workspace.js';
import { $ } from '../shared/dom.js';

export async function boot() {
    const runtime = { session: null, catalog: null };
    const api = createApi(() => runtime.session);
    try {
        runtime.session = await api('/api/session');
        $('account-label').textContent = `${runtime.session.user || 'admin'} · 개인 워크스페이스`;
        runtime.catalog = await api('/api/catalog');
        initPremiums({ api });
        const analysis = initAnalysis({
            api,
            session: runtime.session,
            catalog: runtime.catalog,
            onCatalog: (catalog) => {
                runtime.catalog = catalog;
            },
        });
        await analysis.prepare();
        await initWorkspace({
            api,
            getCatalog: () => runtime.catalog,
            stopLive: analysis.stopLive,
            session: runtime.session,
        });
        await analysis.runHistory();
    } catch (error) {
        $('error').textContent = error.message || String(error);
        $('error').hidden = false;
        $('data-status').textContent = '분석 서버에 연결하지 못했습니다';
        document.querySelector('#chart-placeholder strong').textContent =
            '서버 연결을 확인해주세요';
    }
}
