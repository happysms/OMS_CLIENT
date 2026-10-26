import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

export const { JSDOM } = await import(process.env.OMS_JSDOM_MODULE || 'jsdom');
export const ROOT = fileURLToPath(new URL('../../', import.meta.url)).replace(/\/$/, '');
export const FRONTEND = ROOT + '/frontend/';
export async function loadStrategyCatalog() {
    if (!process.env.OMS_STRATEGY_CATALOG)
        throw new Error(
            'Run through tools/check_frontend.py to generate an isolated strategy catalog.',
        );
    return JSON.parse(await readFile(process.env.OMS_STRATEGY_CATALOG, 'utf8'));
}
