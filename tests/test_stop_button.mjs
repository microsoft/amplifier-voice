// In-process check of the shipped StopButton handler, not a rendered browser test.
// Run: node --test tests/test_stop_button.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const page = readFileSync(new URL('../src/voice_plugin/static/index.html', import.meta.url), 'utf8');
const start = page.indexOf('  function StopButton(');
const end = page.indexOf('\n  // -------------------------------------------------------------------------', start);
assert(start >= 0 && end > start);
const source = page.slice(start, end);

function harness(ok = true) {
  const requests = [];
  const errors = [];
  let now = 1000;
  let handleClick;
  let stopped = 0;
  const StopButton = vm.runInNewContext(`(${source})`, {
    useState: () => [false, () => {}],
    useRef: value => ({ current: value }),
    useCallback: callback => { handleClick = callback; return callback; },
    Date: { now: () => now },
    fetch: async (path, options) => {
      requests.push({ path, ...options, body: JSON.parse(options.body) });
      return { ok, status: ok ? 200 : 403 };
    },
    voiceHeaders: headers => ({ ...headers, 'X-Voice-Token': 'synthetic-token' }),
    console: { error: (...args) => errors.push(args) },
    html: () => null,
  });
  StopButton({
    sessionId: 'active-test-session',
    runningTools: ['delegate'],
    onStop: () => { stopped++; },
  });
  return {
    requests, errors,
    click: async time => { now = time; await handleClick(); },
    stopped: () => stopped,
  };
}

test('Stop button sends authenticated, active-session graceful then immediate cancellation', async () => {
  const h = harness();
  await h.click(1000);
  await h.click(1200);
  assert.deepEqual(h.requests.map(r => r.body), [
    { session_id: 'active-test-session', level: 'graceful' },
    { session_id: 'active-test-session', level: 'immediate' },
  ]);
  assert(h.requests.every(r => r.path === '/voice/cancel' && r.method === 'POST'));
  assert(h.requests.every(r => r.headers['X-Voice-Token'] === 'synthetic-token'));
  assert.equal(h.stopped(), 2);
  assert.equal(h.errors.length, 0);
});

test('Rejected stale-session cancellation does not report onStop success', async () => {
  const h = harness(false);
  await h.click(1000);
  assert.equal(h.stopped(), 0);
  assert.equal(h.errors.length, 1);
});