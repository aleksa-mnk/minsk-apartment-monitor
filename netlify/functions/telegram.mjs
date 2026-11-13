/** Telegram webhook for instant, private bot controls.
 *  Listing scans stay in GitHub Actions. No polling is used once configured.
 */
import { createHash, timingSafeEqual, randomUUID } from 'node:crypto';
import {
  makeGitHubApi, settingsFromState, settingsFromPanel, validSettings,
} from './settings.mjs';

const PANEL_FILE = 'data/panel_settings.json';
const STATE_FILE = 'data/state.json';
const JSON_HEADERS = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' };
const DEFAULTS = { walk_minutes: 20, max_price_usd: 0, rooms: 0, owner_only: false, watching: true };

// ASCII-only source; show the user native Russian text in Telegram.
const alphabet = {
  a: 0x430, b: 0x431, v: 0x432, g: 0x433, d: 0x434, e: 0x435,
  zh: 0x436, z: 0x437, i: 0x438, j: 0x439, k: 0x43a,
  l: 0x43b, m: 0x43c, n: 0x43d, o: 0x43e, p: 0x43f,
  r: 0x440, s: 0x441, t: 0x442, u: 0x443, f: 0x444,
  h: 0x445, ts: 0x446, ch: 0x447, sh: 0x448,
  shch: 0x449, '"': 0x44a, y: 0x44b, "'": 0x44c,
  eh: 0x44d, yu: 0x44e, ya: 0x44f, yo: 0x451,
};
function ru(text) {
  return text.replace(/shch|zh|ts|ch|sh|yu|ya|yo|eh|[a-z'"]/gi, part => {
    const point = alphabet[part.toLowerCase()];
    if (!point) return part;
    const output = String.fromCodePoint(point);
    return part[0] === part[0].toUpperCase() ? output.toUpperCase() : output;
  });
}
function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}
function verify(a, b) {
  const left = Buffer.from(a || '');
  const right = Buffer.from(b || '');
  return left.length === right.length && timingSafeEqual(left, right);
}
function webhookSecret(token) {
  return createHash('sha256').update(`minsk-telegram-webhook-v1:${token}`).digest('hex');
}
function env() {
  const token = process.env.BOT_TOKEN || '';
  const chatId = process.env.TELEGRAM_CHAT_ID || '';
  const repo = process.env.GITHUB_REPO || '';
  const githubToken = process.env.GITHUB_PANEL_TOKEN || '';
  const branch = process.env.GITHUB_BRANCH || 'main';
  if (!/^\d+:[A-Za-z0-9_-]+$/.test(token) || !/^\d+$/.test(chatId) ||
      !/^[\w.-]+\/[\w.-]+$/.test(repo) || !githubToken) return null;
  return { token, chatId, repo, githubToken, branch };
}
function api(config) { return makeGitHubApi({ repo: config.repo, token: config.githubToken, branch: config.branch }); }
async function fetchSettings(gh) {
  const [state, panel] = await Promise.all([gh.get(STATE_FILE), gh.get(PANEL_FILE)]);
  const current = settingsFromPanel(panel?.data) || settingsFromState(state?.data || {});
  return { state: state?.data || {}, panel, settings: current };
}
async function updateSettings(gh, change) {
  for (let retry = 0; retry < 3; retry++) {
    const before = await fetchSettings(gh);
    const modified = validSettings({ ...before.settings, ...change(before.settings) });
    if (!modified) throw new Error('Bad settings');
    if (JSON.stringify(before.settings) === JSON.stringify(modified)) return modified;
    const result = await gh.put(PANEL_FILE, { revision: randomUUID(), settings: modified }, before.panel?.sha);
    if (!result.conflict) return modified;
  }
  throw new Error('Settings conflict');
}
function menuText(s) {
  const price = s.max_price_usd ? `${s.max_price_usd} $` : ru('bez limita');
  const rooms = s.rooms === 0 ? ru('lyubye') : s.rooms === 4 ? '4+' : s.rooms;
  return [
    `<b>${ru('Nastrojki')}</b>`,
    `${ru('Peshkom')}: ${s.walk_minutes} ${ru('min')}  |  ${ru('Byudzhet')}: ${price}`,
    `${ru('Komnaty')}: ${rooms}  |  ${ru('Sobstvennik')}: ${s.owner_only ? ru('da') : ru('net')}`,
    `${ru('Monitoring')}: ${s.watching ? ru('vklyuchen') : ru('vyklyuchen')}`,
  ].join('\n');
}
function menuKeyboard(s, panelUrl) {
  return { inline_keyboard: [
    [
      { text: `${ru('Peshkom')}: ${s.walk_minutes} ${ru('min')}`, callback_data: 'choose:walk' },
      { text: `${ru('Byudzhet')}: ${s.max_price_usd ? `${s.max_price_usd} $` : ru('lyuboj')}`, callback_data: 'choose:budget' },
    ],
    [
      { text: `${ru('Komnaty')}: ${s.rooms || ru('lyubye')}`, callback_data: 'choose:rooms' },
      { text: `${ru('Sobstvennik')}: ${s.owner_only ? ru('da') : ru('net')}`, callback_data: `owner:${s.owner_only ? 'off' : 'on'}` },
    ],
    [{ text: s.watching ? ru("Ostanovit' monitoring") : ru("Vklyuchit' monitoring"), callback_data: `watch:${s.watching ? 'off' : 'on'}` }],
    [{ text: ru("Proverit' kvartiry"), callback_data: 'scan' }],
    [{ text: ru("Panel' nastroek"), url: panelUrl }],
  ] };
}
function chooseKeyboard(kind) {
  let choices = [];
  if (kind === 'walk') choices = [10, 15, 20, 25, 30, 40].map(n => [`${n} ${ru('min')}`, `walk:${n}`]);
  else if (kind === 'budget') choices = [400, 500, 600, 700, 800, 0].map(n => [n ? `${n} $` : ru('Bez limita'), `budget:${n}`]);
  else choices = [0, 1, 2, 3, 4].map(n => [n ? (n === 4 ? '4+' : String(n)) : ru('Lyubye'), `rooms:${n}`]);
  const rows = [];
  for (let i = 0; i < choices.length; i += 3) rows.push(choices.slice(i, i + 3).map(([text, callback_data]) => ({ text, callback_data })));
  rows.push([{ text: ru('Nazad'), callback_data: 'choose:menu' }]);
  return { inline_keyboard: rows };
}
async function telegramCall(config, method, body) {
  const result = await fetch(`https://api.telegram.org/bot${config.token}/${method}`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body), signal: AbortSignal.timeout(12000),
  });
  if (!result.ok) throw new Error(`Telegram ${method} failed: ${result.status}`);
  const data = await result.json();
  if (!data.ok) throw new Error(`Telegram ${method} returned not-ok`);
  return data.result;
}
async function send(config, chat, text, reply_markup = undefined) {
  return telegramCall(config, 'sendMessage', { chat_id: chat, text, parse_mode: 'HTML', ...(reply_markup ? { reply_markup } : {}) });
}
async function editOrSend(config, callback, chat, text, keyboard) {
  const messageId = callback?.message?.message_id;
  if (messageId) {
    try {
      await telegramCall(config, 'editMessageText', { chat_id: chat, message_id: messageId, text, parse_mode: 'HTML', reply_markup: keyboard });
      return;
    } catch (err) {
      // An old message can no longer be edited, or content did not change.
      if (String(err.message).includes('400')) return;
    }
  }
  await send(config, chat, text, keyboard);
}
async function dispatchScan(config) {
  const result = await fetch(`https://api.github.com/repos/${config.repo}/dispatches`, {
    method: 'POST', headers: {
      'Accept': 'application/vnd.github+json', 'Authorization': `Bearer ${config.githubToken}`,
      'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Minsk-Rent-Dashboard',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ event_type: 'minsk_scan' }), signal: AbortSignal.timeout(12000),
  });
  if (result.status !== 204) throw new Error(`GitHub dispatch failed: ${result.status}`);
}
function parseAction(text) {
  const value = (text || '').trim().replace(/\s+/g, ' ');
  const [raw, argument] = value.split(' ');
  const name = raw?.split('@')[0]?.toLowerCase() || '';
  if (['/start','/menu','/help','/status','/search','/walk','/budget','/rooms','/owner','/watch','/stop','/hide'].includes(name)) {
    return { name, argument };
  }
  // Support presses on stale, oversized reply keyboards until /start removes them.
  const minutes = value.match(/(\d{1,2})\s*\u043c\u0438\u043d/i);
  if (minutes) return { name: '/walk', argument: minutes[1] };
  const price = value.match(/(\d{1,6})\s*\$/);
  if (price) return { name: '/budget', argument: price[1] };
  if (value.toLowerCase().includes(ru('sobstvennik'))) return { name: '/owner' };
  return { name: '/menu' };
}
function argumentChange(action, settings) {
  if (action.name === '/owner') {
    if (action.argument && !['on','off'].includes(action.argument)) throw new Error('Bad owner');
    return { owner_only: action.argument ? action.argument === 'on' : !settings.owner_only };
  }
  if (action.name === '/watch') return { watching: true };
  if (action.name === '/stop') return { watching: false };
  const kind = action.name.slice(1);
  if (['walk', 'budget', 'rooms'].includes(kind)) {
    if (!/^\d+$/.test(action.argument || '')) throw new Error('Bad number');
    const n = Number(action.argument);
    if (kind === 'walk' && n >= 5 && n <= 45) return { walk_minutes: n };
    if (kind === 'budget' && n <= 100000) return { max_price_usd: n };
    if (kind === 'rooms' && n <= 4) return { rooms: n };
    throw new Error('Bad number');
  }
  return null;
}
function actionFromCallback(data) {
  const [kind, val] = (data || '').split(':');
  if (kind === 'owner' && ['on','off'].includes(val)) return { name: '/owner', argument: val };
  if (kind === 'watch' && ['on','off'].includes(val)) return { name: val === 'on' ? '/watch' : '/stop' };
  if (['walk','budget','rooms'].includes(kind)) return { name: `/${kind}`, argument: val };
  return null;
}

export default async function handler(request) {
  if (request.method === 'GET') return json({ ok: true, service: 'telegram-webhook', configured: Boolean(env()) });
  if (request.method !== 'POST') return json({ ok: false }, 405);
  const config = env();
  if (!config) return json({ ok: false, error: 'Missing Telegram and GitHub environment variables' }, 503);
  if (!verify(request.headers.get('x-telegram-bot-api-secret-token'), webhookSecret(config.token))) return json({ ok: false }, 403);
  let update;
  try { update = await request.json(); } catch { return json({ ok: false }, 400); }
  const callback = update?.callback_query;
  const message = update?.message;
  const chat = callback?.message?.chat || message?.chat;
  const from = callback?.from || message?.from;
  if (!chat || !from || chat.type !== 'private' || String(chat.id) !== config.chatId || String(from.id) !== config.chatId) {
    return json({ ok: true });
  }
  const gh = api(config);
  const panelUrl = new URL(request.url).origin;
  try {
    // Acknowledge callback immediately; a spinner must not wait for GitHub API.
    if (callback) {
      try { await telegramCall(config, 'answerCallbackQuery', { callback_query_id: callback.id }); }
      catch { /* stale callbacks still get handled */ }
    }
    const rawAction = callback ? actionFromCallback(callback.data) : parseAction(message?.text);
    const action = rawAction || { name: '/menu' };
    if (action.name === '/start' || action.name === '/hide') {
      // This removes the old large keyboard once. The following message uses
      // only a compact inline keyboard, not ReplyKeyboardMarkup.
      await send(config, chat.id, ru("Bol'shaya klaviatura skryta."), { remove_keyboard: true });
      if (action.name === '/hide') return json({ ok: true });
    }
    if (callback?.data?.startsWith('choose:') && callback.data !== 'choose:menu') {
      const kind = callback.data.slice(7);
      if (['walk','budget','rooms'].includes(kind)) {
        const intro = kind === 'walk' ? ru('Vremya peshkom') : kind === 'budget' ? ru('Byudzhet') : ru('Komnaty');
        await editOrSend(config, callback, chat.id, `<b>${intro}</b>`, chooseKeyboard(kind));
        return json({ ok: true });
      }
    }
    if (action.name === '/search' || callback?.data === 'scan') {
      await dispatchScan(config);
      await send(config, chat.id, ru('Proverka zapushchena. Ob"yavleniya pridut otdel"no.'));
      return json({ ok: true });
    }
    if (action.name === '/status' || callback?.data === 'status') {
      const state = await fetchSettings(gh);
      const last = state.state.last_scan_at || ru('eshche ne bylo');
      await send(config, chat.id, `${ru('Poslednyaya proverka')}: ${last}\n${menuText(state.settings)}`);
      return json({ ok: true });
    }
    if (['/walk','/budget','/rooms'].includes(action.name) && !action.argument) {
      const kind = action.name.slice(1);
      const intro = kind === 'walk' ? ru('Vremya peshkom') : kind === 'budget' ? ru('Byudzhet') : ru('Komnaty');
      await editOrSend(config, callback, chat.id, `<b>${intro}</b>`, chooseKeyboard(kind));
      return json({ ok: true });
    }
    if (['/owner','/watch','/stop','/walk','/budget','/rooms'].includes(action.name)) {
      let settings;
      try { settings = await updateSettings(gh, current => argumentChange(action, current)); }
      catch (e) {
        if (e.message.startsWith('Bad')) {
          await send(config, chat.id, `${ru('Nevernoe znachenie.') } /walk 18 | /budget 600 | /rooms 1`);
          return json({ ok: true });
        }
        throw e;
      }
      await editOrSend(config, callback, chat.id, menuText(settings), menuKeyboard(settings, panelUrl));
      return json({ ok: true });
    }
    const state = await fetchSettings(gh);
    await editOrSend(config, callback, chat.id, menuText(state.settings), menuKeyboard(state.settings, panelUrl));
    return json({ ok: true });
  } catch (error) {
    console.error('Telegram webhook processing failed:', error.message);
    try { await send(config, chat.id, ru("Ne udalos' vypolnit' dejstvie. Prover' nastrojki.")); }
    catch { /* Telegram may be unavailable; keep webhook response private. */ }
    return json({ ok: true, error: 'Action failed' });
  }
}
