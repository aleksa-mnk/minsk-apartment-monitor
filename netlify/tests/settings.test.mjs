import assert from 'node:assert/strict';
import test from 'node:test';
import handler from '../functions/settings.mjs';

const TEST_PASSWORD = 'not-a-real-secret-password';
const TEST_TOKEN = 'fake-token';
const expected = { walk_minutes: 20, max_price_usd: 600, rooms: 1, owner_only: true, watching: true };

function setup(t, apiFetch) {
  const oldFetch = globalThis.fetch;
  const saved = {};
  for (const key of ['PANEL_PASSWORD', 'GITHUB_PANEL_TOKEN', 'GITHUB_REPO', 'GITHUB_BRANCH']) {
    saved[key] = process.env[key];
  }
  process.env.PANEL_PASSWORD = TEST_PASSWORD;
  process.env.GITHUB_PANEL_TOKEN = TEST_TOKEN;
  process.env.GITHUB_REPO = 'testowner/testrepo';
  process.env.GITHUB_BRANCH = 'main';
  globalThis.fetch = apiFetch;
  t.after(() => {
    globalThis.fetch = oldFetch;
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
}

function req(method, data, token = TEST_PASSWORD) {
  return new Request('https://sample.netlify.app/.netlify/functions/settings', {
    method,
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
}

function githubResponse(data, sha = 'sha123') {
  return new Response(JSON.stringify({ sha, encoding: 'base64', content: Buffer.from(JSON.stringify(data)).toString('base64') }), {
    status: 200, headers: { 'content-type': 'application/json' },
  });
}

function githubFake({ state = {}, panel = null, capture = () => {} } = {}) {
  return async (url, options = {}) => {
    assert.equal(options.headers.Authorization, `Bearer ${TEST_TOKEN}`);
    if (options.method === 'PUT') {
      const payload = JSON.parse(options.body);
      capture({ url, payload });
      return new Response('{}', { status: 201 });
    }
    if (url.includes('/data/state.json')) return githubResponse(state);
    if (url.includes('/data/panel_settings.json')) return panel ? githubResponse(panel) : new Response('', { status: 404 });
    throw new Error(`Unexpected URL ${url}`);
  };
}

test('rejects wrong password before touching GitHub', async t => {
  setup(t, () => { throw new Error('Should not reach GitHub'); });
  const result = await handler(req('GET', undefined, 'wrong-password'));
  assert.equal(result.status, 401);
  assert.equal(result.headers.get('cache-control'), 'no-store');
});

test('requires server-side configuration', async t => {
  setup(t, () => { throw new Error('Should not reach GitHub'); });
  delete process.env.GITHUB_PANEL_TOKEN;
  const result = await handler(req('GET'));
  assert.equal(result.status, 503);
});

test('reads existing Telegram settings when panel was not saved yet', async t => {
  setup(t, githubFake({ state: { ...expected, last_scan_at: '2026-11-13T15:00:00+00:00' } }));
  const result = await handler(req('GET'));
  assert.equal(result.status, 200);
  const data = await result.json();
  assert.deepEqual(data.settings, expected);
  assert.equal(data.revision, null);
  assert.equal(data.pending, false);
  assert.equal(data.last_scan_at, '2026-11-13T15:00:00+00:00');
});

test('new panel revision takes precedence until next scheduled run', async t => {
  const panel = { revision: 'rev2', settings: { ...expected, walk_minutes: 35 } };
  setup(t, githubFake({ state: { ...expected, panel_revision: 'rev1' }, panel }));
  const result = await handler(req('GET'));
  const data = await result.json();
  assert.equal(data.settings.walk_minutes, 35);
  assert.equal(data.pending, true);
  assert.equal(data.revision, 'rev2');
});

test('after panel was applied, shows newer Telegram command filters', async t => {
  const panel = { revision: 'rev2', settings: { ...expected, walk_minutes: 35 } };
  setup(t, githubFake({ state: { ...expected, panel_revision: 'rev2', walk_minutes: 18 }, panel }));
  const result = await handler(req('GET'));
  const data = await result.json();
  assert.equal(data.settings.walk_minutes, 18);
  assert.equal(data.pending, false);
});

test('saves valid settings in a separate GitHub file and returns revision', async t => {
  let captured;
  setup(t, githubFake({ capture: item => { captured = item; } }));
  const settings = { ...expected, walk_minutes: 15 };
  const result = await handler(req('PUT', { expectedRevision: null, settings }));
  assert.equal(result.status, 200);
  const body = await result.json();
  assert.equal(body.pending, true);
  assert.equal(body.settings.walk_minutes, 15);
  assert.match(body.revision, /^[0-9a-f-]{36}$/);
  assert.equal(captured.payload.branch, 'main');
  assert.equal(captured.payload.sha, undefined);
  assert.equal(JSON.parse(Buffer.from(captured.payload.content, 'base64').toString()).settings.walk_minutes, 15);
  assert.match(captured.url, /panel_settings\.json/);
});

test('prevents overwriting a newer version from another tab', async t => {
  const panel = { revision: 'newer', settings: expected };
  setup(t, githubFake({ state: { ...expected }, panel, capture: () => { throw new Error('Unexpected write'); } }));
  const result = await handler(req('PUT', { expectedRevision: 'older', settings: expected }));
  assert.equal(result.status, 409);
});

test('rejects unsafe settings before writing to GitHub', async t => {
  setup(t, githubFake());
  for (const settings of [
    { ...expected, walk_minutes: 999 },
    { ...expected, max_price_usd: -100 },
    { ...expected, owner_only: 'true' },
    { ...expected, walk_minutes: true },
  ]) {
    const result = await handler(req('PUT', { expectedRevision: null, settings }));
    assert.equal(result.status, 400);
  }
});
