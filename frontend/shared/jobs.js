import { $, wait } from './dom.js';

export function createJobRunner({ api, prefix, getVersion }) {
    return async function job(path, body, version) {
        const created = await api(path, { method: 'POST', body }),
            began = Date.now();
        for (;;) {
            if (version !== getVersion()) return null;
            const response = await api(path + '/' + encodeURIComponent(created.id));
            if (version !== getVersion()) return null;
            if (['complete', 'succeeded'].includes(response.status)) {
                if (!response.result)
                    throw new Error(
                        '결과 보관 기간이 지났습니다. 같은 조건으로 다시 실행해주세요.',
                    );
                return { id: created.id, result: response.result };
            }
            if (['failed', 'cancelled', 'stopped'].includes(response.status))
                throw new Error(
                    typeof response.error === 'string'
                        ? response.error
                        : response.error?.message || '서버 계산을 완료하지 못했습니다.',
                );
            $(prefix + '-message').textContent =
                `${response.status === 'queued' ? '서버 작업 대기 중' : '서버에서 계산 중'} · ${Math.floor((Date.now() - began) / 1000)}초`;
            if (Date.now() - began > 600000)
                throw new Error('계산 대기가 길어지고 있습니다. 잠시 후 다시 실행해주세요.');
            await wait(500);
        }
    };
}
