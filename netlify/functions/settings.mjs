/**
 * Private Netlify dashboard API. Authentication and GitHub token stay server-side.
 * Panel writes data/panel_settings.json; Actions owns data/state.json.
 * Separating files prevents write conflicts with scheduled scans.
 */
import { timingSafeEqual, randomUUID } from 'node:crypto';

const DEFAULTS = Object.freeze({
  walk_minutes: 20,
  max_price_usd: 0,
  rooms: 0,
  owner_only: false,
  watching: true,
});
const FILTER_FIELDS = Object.keys(DEFAULTS);
const PANEL_FILE = 'data/panel_settings.json';
const STATE_FILE = 'data/state.json';
const NO_STORE = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };

function response(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: NO_STORE });
}

function constantTimeEquals(a, b) {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

export function validSettings(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  if (Object.keys(value).sort().join('|') !== FILTER_FIELDS.slice().sort().join('|')) return null;
  const { walk_minutes, max_price_usd, rooms, owner_only, watching } = value;
  if (!Number.isInteger(walk_minutes) || walk_minutes < 5 || walk_minutes > 45) return null;
  if (!Number.isInteger(max_price_usd) || max_price_usd < 0 || max_price_usd > 100000) return null;
  if (!Number.isInteger(rooms) || rooms < 0 || rooms > 4) return null;
  if (typeof owner_only !== 'boolean' || typeof watching !== 'boolean') return null;
  return { walk_minutes, max_price_usd, rooms, owner_only, watching };
}

export function settingsFromState(state) {
  const candidate = Object.fromEntries(
    FILTER_FIELDS.map(key => [key, state && Object.hasOwn(state, key) ? state[key] : DEFAULTS[key]])
  );
  return validSettings(candidate) || { ...DEFAULTS };
}

export function settingsFromPanel(panel) {
  if (!panel || typeof panel.revision !== 'string' || !panel.revision) return null;
  return validSettings(panel.settings);
}

export function makeGitHubApi({ repo, token, branch }) {
  const apiUrl = `https://api.github.com/repos/${repo}/contents/`;
  const headers = {
    Accept: 'application/vnd.github+json',
    Authorization: `Bearer ${token}`,
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'Minsk-Rent-Dashboard',
  };
  return {
    async get(path) {
      const result = await fetch(`${apiUrl}${path}?ref=${encodeURIComponent(branch)}`, {
        headers, signal: AbortSignal.timeout(15000),
      });
      if (result.status === 404) return null;
      if (!result.ok) throw new Error(`GitHub read failed: ${result.status}`);
      const file = await result.json();
      if (file.encoding !== 'base64' || typeof file.content !== 'string') throw new Error('Unexpected GitHub file encoding');
      const obj = JSON.parse(Buffer.from(file.content, 'base64').toString('utf8'));
      return { data: obj, sha: file.sha };
    },
    async put(path, obj, sha) {
      const payload = {
        message: 'Update apartment dashboard settings [skip ci]',
        content: Buffer.from(`${JSON.stringify(obj, null, 2)}\n`, 'utf8').toString('base64'),
        branch,
      };
      if (sha) payload.sha = sha;
      const result = await fetch(`${apiUrl}${path}`, {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(15000),
      });
      if (result.status === 409 || result.status === 422) return { conflict: true };
      if (!result.ok) throw new Error(`GitHub write failed: ${result.status}`);
      return { conflict: false };
    },
  };
}

export default async function handler(request) {
  if (request.method !== 'GET' && request.method !== 'PUT') return response({ error: 'Метод не поддерживается.' }, 405);
  const secret = process.env.PANEL_PASSWORD || '';
  const token = process.env.GITHUB_PANEL_TOKEN || '';
  const repo = process.env.GITHUB_REPO || '';
  const branch = process.env.GITHUB_BRANCH || 'main';
  if (!secret || !token || !/^[\w.-]+\/[\w.-]+$/.test(repo)) {
    return response({ error: 'Укажи PANEL_PASSWORD, GITHUB_PANEL_TOKEN и GITHUB_REPO в настройках Netlify.' }, 503);
  }
  if (secret.length < 12) return response({ error: 'Пароль панели должен быть не короче 12 символов.' }, 503);
  const provided = request.headers.get('authorization') || '';
  if (!provided.startsWith('Bearer ') || !constantTimeEquals(provided.slice(7), secret)) {
    return response({ error: 'Неверный пароль.' }, 401);
  }

  let incoming = null;
  if (request.method === 'PUT') {
    try { incoming = await request.json(); } catch { return response({ error: 'Неверный формат запроса.' }, 400); }
    const validRevision = incoming && (incoming.expectedRevision === null ||
      (typeof incoming.expectedRevision === 'string' && incoming.expectedRevision.length <= 100));
    if (!validRevision) {
      return response({ error: 'Обнови страницу и повтори попытку.' }, 400);
    }
    const valid = validSettings(incoming.settings);
    if (!valid) return response({ error: 'Проверь время, бюджет и остальные настройки.' }, 400);
    incoming.settings = valid;
  }

  try {
    const github = makeGitHubApi({ repo, token, branch });
    const [stateFile, panelFile] = await Promise.all([
      github.get(STATE_FILE), github.get(PANEL_FILE),
    ]);
    const state = stateFile?.data || {};
    const panel = panelFile?.data || null;
    const panelSettings = settingsFromPanel(panel);
    const savedRevision = panelSettings ? panel.revision : null;

    if (request.method === 'GET') {
      const pending = Boolean(savedRevision && state.panel_revision !== savedRevision);
      const settings = pending ? panelSettings : settingsFromState(state);
      return response({
        settings,
        revision: savedRevision,
        pending,
        last_scan_at: typeof state.last_scan_at === 'string' ? state.last_scan_at : null,
      });
    }

    if (incoming.expectedRevision !== savedRevision) {
      return response({ error: 'Настройки были изменены. Обнови страницу.' }, 409);
    }
    const newPanel = { revision: randomUUID(), settings: incoming.settings };
    const write = await github.put(PANEL_FILE, newPanel, panelFile?.sha);
    if (write.conflict) return response({ error: 'Настройки обновились в другом окне. Попробуй ещё раз.' }, 409);
    return response({ settings: newPanel.settings, revision: newPanel.revision, pending: true });
  } catch (error) {
    // Never leak repository content, token, or request authorization.
    console.error('Dashboard GitHub API error:', error.message);
    return response({ error: 'GitHub временно недоступен или неверно настроен доступ к репозиторию.' }, 502);
  }
}
