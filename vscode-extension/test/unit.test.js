'use strict';
const test = require('node:test');
const assert = require('node:assert');
const http = require('http');
const os = require('os');
const fs = require('fs');
const path = require('path');

const fmt = require('../lib/format');
const { GuildClient, parseSseBlock } = require('../lib/client');
const server = require('../lib/server');

test('eventLine renders the events the server sends and ignores tokens', () => {
  assert.match(fmt.eventLine({ kind: 'phase', phase: 'implement', task_id: 'T1', round: 2, ts: 1 }).text, /▶ implement T1 \(round 2\)/);
  assert.match(fmt.eventLine({ kind: 'verification', passed: false, tests_run: 3, failures: ['a', 'b'] }).text, /tests: FAIL \(3 run\) — a; b/);
  assert.match(fmt.eventLine({ kind: 'tool_call', role: 'engineer', tool: 'write_file', ok: true, args: { path: 'a.py' }, result_preview: 'created\na.py' }).text, /engineer write_file\(path="a.py"\) → created a.py/);
  assert.match(fmt.eventLine({ kind: 'task_end', task_id: 'T1', accepted: true, rounds: 1, cost_usd: 0, branch: 'guild/t1-x' }).text, /✔ task T1 ACCEPTED .* branch guild\/t1-x/);
  assert.match(fmt.eventLine({ kind: 'job_error', job: 'run', error: 'boom' }).text, /✖ run failed: boom/);
  assert.match(fmt.eventLine({ kind: 'approval_request', request: { kind: 'edit', path: 'a.py', new_file: true } }).text, /waiting for approval: create a.py/);
  assert.match(fmt.eventLine({ kind: 'approval', approved: false, tool: 'run_command', target: 'rm -rf x' }).text, /declined: run_command rm -rf x/);
  assert.match(fmt.eventLine({ kind: 'job_cancelled', job: 'pair' }).text, /pair cancelled/);
  assert.strictEqual(fmt.eventLine({ kind: 'verification', passed: false, tests_run: 1 }).cls, 'bad');
  assert.strictEqual(fmt.eventLine({ kind: 'task_end', task_id: 'T1', accepted: true, rounds: 1, cost_usd: 0 }).cls, 'ok');
  assert.strictEqual(fmt.eventLine({ kind: 'phase', phase: 'plan' }).cls, 'phase');
  assert.strictEqual(fmt.eventLine({ kind: 'report', level: 'fallback', msg: 'x' }), null);
  assert.strictEqual(fmt.eventLine({ kind: 'token', role: 'lead', text: 'x' }), null);
  assert.strictEqual(fmt.eventLine({ kind: 'report', level: 'phase', msg: 'x' }), null);
  assert.strictEqual(fmt.eventLine({ kind: 'something_new' }), null);
});

test('taskView encodes status and branch state for menus', () => {
  const todo = fmt.taskView({ id: 'T1', title: 'a', status: 'todo', depends_on: ['T0'] });
  assert.strictEqual(todo.contextValue, 'task todo');
  assert.match(todo.description, /after T0/);
  const ready = fmt.taskView({ id: 'T1', title: 'a', status: 'done', branch: 'guild/t1', base: 'main' });
  assert.strictEqual(ready.contextValue, 'task done hasBranch');
  assert.deepStrictEqual(ready.details.find(([k]) => k === 'Branch'), ['Branch', 'guild/t1 (from main)']);
  const merged = fmt.taskView({ id: 'T1', title: 'a', status: 'done', branch: 'guild/t1', merged: true });
  assert.strictEqual(merged.contextValue, 'task done merged');
  assert.strictEqual(merged.icon, 'git-merge');
  // unknown status from a hand-edited plan must not break the tree
  assert.strictEqual(fmt.taskView({ id: 'T9', status: 'banana' }).contextValue, 'task todo');
  // a stale in_progress (server restarted) is shown as waiting, not spinning
  assert.match(fmt.taskView({ id: 'T1', status: 'in_progress' }, false).description, /interrupted/);
  assert.strictEqual(fmt.taskView({ id: 'T1', status: 'in_progress' }, true).icon, 'sync~spin');
});

test('diffFiles lists added, deleted and modified files', () => {
  const diff = [
    'diff --git a/app.py b/app.py', 'index 1..2 100644', '--- a/app.py', '+++ b/app.py', '@@ -1 +1 @@', '-a', '+b',
    'diff --git a/new file.py b/new file.py', 'new file mode 100644', '+x',
    'diff --git a/old.py b/old.py', 'deleted file mode 100644', '-x',
  ].join('\n');
  assert.deepStrictEqual(fmt.diffFiles(diff), [
    { path: 'app.py', status: 'modified' },
    { path: 'new file.py', status: 'added' },
    { path: 'old.py', status: 'deleted' },
  ]);
  assert.deepStrictEqual(fmt.diffFiles(''), []);
});

test('parseSseBlock ignores comments, retry lines and broken JSON', () => {
  assert.deepStrictEqual(parseSseBlock('retry: 2000'), []);
  assert.deepStrictEqual(parseSseBlock(': keepalive'), []);
  assert.deepStrictEqual(parseSseBlock('data: {"kind":"note"}'), [{ kind: 'note' }]);
  assert.deepStrictEqual(parseSseBlock('data: {"kind":'), []);
});

test('splitCommand honours quotes; candidates prefer the configured command', () => {
  assert.deepStrictEqual(server.splitCommand('"C:\\My Tools\\guild.exe" --x'), ['C:\\My Tools\\guild.exe', '--x']);
  assert.deepStrictEqual(server.splitCommand('py -m guild.cli'), ['py', '-m', 'guild.cli']);
  assert.deepStrictEqual(server.splitCommand(''), []);
  const c = server.candidates({ workspace: os.tmpdir(), configured: 'py -m guild.cli', python: '/usr/bin/python3' });
  assert.deepStrictEqual(c[0], ['py', '-m', 'guild.cli']);
  assert.deepStrictEqual(c[c.length - 1], ['guild']);
  assert.ok(c.some((a) => a.join(' ') === '/usr/bin/python3 -m guild.cli'));
});

test('client: errors carry the server detail; replayed and repeated events are handled', async () => {
  let streams = 0;
  const srv = http.createServer((req, res) => {
    if (req.url === '/api/events') {
      streams += 1;
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('retry: 2000\n\n');
      const old = Date.now() / 1000 - 3600;
      res.write(`data: ${JSON.stringify({ kind: 'job_error', job: 'run', error: 'old', ts: old })}\n\n`);
      res.write(`data: ${JSON.stringify({ kind: 'note', msg: 'fresh' + streams, ts: Date.now() / 1000 + streams })}\n\n`);
      setTimeout(() => res.end(), 50); // drop the connection: the client must reconnect
      return;
    }
    if (req.url === '/busy') {
      res.writeHead(409, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ detail: 'a run job is already running' }));
      return;
    }
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ method: req.method, body: raw ? JSON.parse(raw) : null, origin: req.headers.origin || null }));
    });
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const client = new GuildClient(`http://127.0.0.1:${srv.address().port}`);
  try {
    const echoed = await client.post('/x', { a: 1 });
    assert.deepStrictEqual(echoed, { method: 'POST', body: { a: 1 }, origin: null }); // no Origin header
    await assert.rejects(client.get('/busy'), (e) => e.status === 409 && /already running/.test(e.message));

    const seen = [];
    client.on('event', (ev, replayed) => seen.push([ev.msg || ev.error, replayed]));
    client.connectEvents();
    const t0 = Date.now();
    while (streams < 2 || seen.length < 3) {
      assert.ok(Date.now() - t0 < 8000, `no reconnect: ${JSON.stringify(seen)}`);
      await new Promise((r) => setTimeout(r, 50));
    }
    // first connect: history kept but flagged; after the reconnect the old event is dropped
    assert.deepStrictEqual(seen.slice(0, 3), [['old', true], ['fresh1', false], ['fresh2', false]]);
  } finally {
    client.close();
    srv.close();
  }
});

test('server.start reports every command it tried when none works', async () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'guild-empty-'));
  const saved = process.env.PATH;
  process.env.PATH = empty;
  try {
    await assert.rejects(
      server.start({ workspace: empty, configured: 'no-such-guild-binary --flag', timeoutMs: 3000 }),
      (e) => e.tried.length === 2 && /no-such-guild-binary --flag: command not found/.test(e.message) && /guild: command not found/.test(e.message)
    );
  } finally {
    process.env.PATH = saved;
  }
});
