'use strict';
// End to end: the real extension code (against a stand-in for the `vscode` module) starts a real
// `guild ui` server, which talks to a scripted OpenAI-compatible model. Plans, runs a task on a
// git branch, reviews the diff, merges, chats, then stops.
//
// Needs the `guild` command (pip install -e ".[dev]"). Skipped when it is not installed.

const test = require('node:test');
const assert = require('node:assert');
const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');

const mockPath = path.join(__dirname, 'mock-vscode.js');
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  return request === 'vscode' ? mockPath : origResolve.call(this, request, ...rest);
};
const vscode = require('vscode');
const mock = vscode.__mock;
const fakeModel = require('./fake-model');

const GUILD = process.env.GUILD_COMMAND || 'guild';
const hasGuild = cp.spawnSync(GUILD, ['--version'], { shell: process.platform === 'win32' }).status === 0;

function git(cwd, ...args) {
  const r = cp.spawnSync('git', args, { cwd, encoding: 'utf8' });
  assert.strictEqual(r.status, 0, `git ${args.join(' ')}: ${r.stderr}`);
  return r.stdout.trim();
}

function makeProject() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'guild-vsc-')));
  fs.writeFileSync(path.join(dir, 'app.py'), 'def add(a, b):\n    return a + b\n');
  fs.writeFileSync(path.join(dir, 'test_app.py'), 'from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n');
  fs.writeFileSync(path.join(dir, '.gitignore'), '.guild/\n__pycache__/\n.pytest_cache/\n');
  fs.mkdirSync(path.join(dir, '.guild', 'profiles'), { recursive: true });
  fs.writeFileSync(
    path.join(dir, '.guild', 'config.yaml'),
    'profile: e2e\ntest_command: python -m pytest -q -p no:cacheprovider\nsandbox: local\n'
  );
  const slots = ['frontier', 'coder', 'reasoner', 'cheap'].map((s) => `  ${s}: [custom/fake]`).join('\n');
  fs.writeFileSync(
    path.join(dir, '.guild', 'profiles', 'e2e.yaml'),
    `name: e2e\nslots:\n${slots}\nlimits:\n  max_revision_rounds: 2\n  max_usd_per_run: 0\n`
  );
  git(dir, 'init', '-q', '-b', 'main');
  git(dir, 'config', 'user.email', 't@t');
  git(dir, 'config', 'user.name', 't');
  git(dir, 'add', '-A');
  git(dir, 'commit', '-q', '-m', 'init');
  return dir;
}

async function until(what, fn, ms = 90000) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > ms) {
      throw new Error(`timed out waiting for ${what}\n--- log ---\n${(mock.channel ? mock.channel.lines : []).slice(-40).join('\n')}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
}

function taskElement() {
  return mock.tree.provider.getChildren()[0];
}

test('extension end to end against a real guild server', { skip: !hasGuild && 'guild is not installed', timeout: 240000 }, async (t) => {
  const model = await fakeModel.start();
  process.env.CUSTOM_OPENAI_BASE_URL = model.url; // read by the guild server at start-up
  const project = makeProject();
  mock.workspaceFolder = project;
  mock.config.command = GUILD === 'guild' ? '' : GUILD;

  const webviewOut = [];
  let toWebview;
  const fakeView = {
    webview: {
      options: {},
      cspSource: 'vscode-webview:',
      html: '',
      asWebviewUri: (u) => 'vscode-resource:' + u.fsPath,
      postMessage: (m) => webviewOut.push(m),
      onDidReceiveMessage: (fn) => (toWebview = fn),
    },
    onDidDispose: () => {},
  };

  const ext = require('../extension');
  const context = { subscriptions: [], extensionUri: vscode.Uri.file(path.join(__dirname, '..')) };
  const ctl = await ext.activate(context);
  t.after(() => {
    ext.deactivate();
    model.srv.close();
  });

  await t.test('starts the server for a folder that has .guild/config.yaml', async () => {
    assert.strictEqual(ctl.status, 'running', mock.channel.lines.join('\n'));
    assert.strictEqual(mock.context['guild.state'], 'running');
    assert.strictEqual(mock.context['guild.hasPlan'], false);
    assert.strictEqual(ctl.state.root, project);
    assert.match(mock.bar.text, /guild/);
  });

  await t.test('chat view renders with a strict CSP and receives status', async () => {
    mock.webviewProvider.resolveWebviewView(fakeView);
    assert.match(fakeView.webview.html, /default-src 'none'/);
    assert.doesNotMatch(fakeView.webview.html, /http:\/\/127\.0\.0\.1/); // the webview never calls the server
    await toWebview({ type: 'ready' });
    const status = webviewOut.find((m) => m.type === 'status');
    assert.ok(status && status.ready && !status.busy, JSON.stringify(webviewOut));
    assert.ok(webviewOut.some((m) => m.type === 'history' && m.items.length === 0));
  });

  await t.test('New Plan asks for a goal and shows the tasks', async () => {
    mock.onInput = () => 'add a subtract function';
    await vscode.commands.executeCommand('guild.plan');
    await until('plan', () => !ctl.busy && ctl.state.plan && ctl.state.plan.tasks.length === 1);
    assert.strictEqual(mock.context['guild.hasPlan'], true);
    const el = taskElement();
    const item = mock.tree.provider.getTreeItem(el);
    assert.strictEqual(item.label, 'T1  add subtract');
    assert.strictEqual(item.contextValue, 'task todo');
    assert.match(mock.tree.message, /add a subtract function/);
    const details = mock.tree.provider.getChildren(el).map((d) => mock.tree.provider.getTreeItem(d).label);
    assert.ok(details.some((d) => d.startsWith('Done when: test_subtract passes')), details.join('|'));
    await until('plan notification', () => mock.messages.some((m) => /plan ready — 1 task/.test(m.message)));
  });

  await t.test('Run Task implements on a branch, verifies, reviews and reports', async () => {
    await vscode.commands.executeCommand('guild.runTask', taskElement());
    assert.strictEqual(mock.context['guild.busy'], true);
    await until('task done', () => !ctl.busy && ctl.state.plan.tasks[0].status === 'done');
    const task = ctl.state.plan.tasks[0];
    assert.match(task.branch, /^guild\/t1-add-subtract/);
    assert.strictEqual(task.base, 'main');
    assert.strictEqual(git(project, 'rev-parse', '--abbrev-ref', 'HEAD'), task.branch);
    assert.match(git(project, 'ls-tree', '-r', '--name-only', 'HEAD'), /calc\.py[\s\S]*test_calc\.py/);
    const log = mock.channel.lines.join('\n');
    assert.match(log, /▶ implement T1/);
    assert.match(log, /engineer write_file\(path="calc.py"/);
    assert.match(log, /engineer write_file\(path="test_calc.py"/); // both index-0 tool calls arrived
    assert.match(log, /tests: PASS \(2 run\)/);
    assert.match(log, /reviewers see \d+ chars of diff: calc.py, test_calc.py/);
    assert.match(log, /✔ task T1 ACCEPTED/);
    assert.ok(webviewOut.some((m) => m.type === 'token'), 'model output is streamed to the view');
    assert.ok(webviewOut.some((m) => m.type === 'log'));
    const note = mock.messages.find((m) => /T1 accepted/.test(m.message));
    assert.deepStrictEqual(note.actions, ['Review Changes', 'Merge']);
    assert.strictEqual(mock.tree.provider.getTreeItem(taskElement()).contextValue, 'task done hasBranch');
  });

  await t.test('Review Changes shows the unified diff, or a native side-by-side diff per file', async () => {
    mock.onQuickPick = (items) => items[0]; // "Whole change"
    await vscode.commands.executeCommand('guild.diffTask', taskElement());
    const doc = mock.shownDocs[mock.shownDocs.length - 1];
    assert.strictEqual(doc.uri.path, '/diff/T1.diff');
    assert.match(doc.getText(), /\+def subtract\(a, b\):/);

    mock.extensions['vscode.git'] = {
      isActive: true,
      exports: { getAPI: () => ({ toGitUri: (uri, ref) => `${ref}:${path.basename(uri.fsPath)}` }) },
    };
    mock.onQuickPick = (items) => items.find((i) => i.file && i.file.path === 'calc.py');
    const before = mock.executed.length;
    await vscode.commands.executeCommand('guild.diffTask', taskElement());
    const call = mock.executed.slice(before).find((c) => c[0] === 'vscode.diff');
    assert.ok(call, 'vscode.diff was opened');
    assert.strictEqual(call[1], 'main:calc.py');
    assert.strictEqual(call[2], `${ctl.state.plan.tasks[0].branch}:calc.py`);
  });

  await t.test('Merge asks first, then merges into the branch the task started from', async () => {
    mock.onMessage = () => undefined; // user cancels the modal
    await vscode.commands.executeCommand('guild.mergeTask', taskElement());
    assert.notStrictEqual(git(project, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main');

    mock.onMessage = (message, actions) => (actions.includes('Merge') ? 'Merge' : undefined);
    await vscode.commands.executeCommand('guild.mergeTask', taskElement());
    assert.strictEqual(git(project, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main');
    assert.ok(fs.existsSync(path.join(project, 'calc.py')));
    assert.ok(mock.messages.some((m) => m.level === 'info' && /merged guild\/t1-add-subtract.* into main/.test(m.message)));
    assert.strictEqual(mock.tree.provider.getTreeItem(taskElement()).contextValue, 'task done merged');
    mock.onMessage = () => undefined;
  });

  await t.test('chat goes through the extension and the answer comes back', async () => {
    const before = webviewOut.length;
    await toWebview({ type: 'send', text: 'what is in this project?' });
    await until('chat answer', () => webviewOut.slice(before).some((m) => m.type === 'message' && m.cls === 'assistant'));
    const msgs = webviewOut.slice(before).filter((m) => m.type === 'message');
    assert.strictEqual(msgs[0].cls, 'user');
    assert.match(msgs[msgs.length - 1].text, /app\.py/);
    assert.match(msgs[msgs.length - 1].role, /custom\/fake/);
    await until('idle', () => !ctl.busy);
  });

  await t.test('a failing job surfaces as an error, not silence', async () => {
    const before = mock.messages.length;
    await vscode.commands.executeCommand('guild.runTask', { type: 'task', task: { id: 'NOPE' } });
    await until('job error', () => mock.messages.slice(before).some((m) => m.level === 'error' && /no task NOPE/.test(m.message)));
    await until('idle', () => !ctl.busy);
  });

  await t.test('editing a task and the doctor/trace views work', async () => {
    const answers = ['add subtract (edited)', 'new description', 'test_subtract passes'];
    mock.onInput = () => answers.shift();
    await vscode.commands.executeCommand('guild.editTask', taskElement());
    assert.strictEqual(ctl.state.plan.tasks[0].title, 'add subtract (edited)');

    await vscode.commands.executeCommand('guild.doctor');
    assert.ok(mock.channel.lines.some((l) => /custom\/fake/.test(l) && /configured/.test(l)));

    mock.onQuickPick = (items) => items.find((i) => /implement/.test(i.detail || '')) || items[0];
    await vscode.commands.executeCommand('guild.showRuns');
    const doc = mock.shownDocs[mock.shownDocs.length - 1];
    assert.match(doc.uri.path, /^\/runs\/.+\.log$/);
    assert.match(doc.getText(), /engineer\s+\d+ calls/);
  });

  await t.test('pair mode: a declined change is not made (answered from the chat view)', async () => {
    mock.onMessage = () => undefined; // the notification is ignored; the card is used instead
    const before = webviewOut.length;
    await toWebview({ type: 'send', text: 'make hello.py', mode: 'ask' });
    const card = await until('approval card', () => webviewOut.slice(before).find((m) => m.type === 'approval'));
    assert.strictEqual(card.request.kind, 'edit');
    assert.strictEqual(card.request.path, 'hello.py');
    assert.match(card.request.preview, /\+print\('hello'\)/);
    assert.ok(mock.messages.some((m) => m.message === 'Guild wants to create hello.py' && m.actions.includes('Open Diff')));
    assert.match(mock.bar.tooltip, /waiting for your approval/);
    assert.ok(!fs.existsSync(path.join(project, 'hello.py')), 'nothing is written before the answer');
    await toWebview({ type: 'approve', id: card.request.id, approve: false });
    await until('reply', () => webviewOut.slice(before).some((m) => m.type === 'message' && /left it alone/.test(m.text)));
    assert.ok(webviewOut.slice(before).some((m) => m.type === 'approvalDone' && m.id === card.request.id));
    assert.ok(!fs.existsSync(path.join(project, 'hello.py')));
    await until('idle', () => !ctl.busy);
    assert.match(mock.channel.lines.join('\n'), /declined: write_file hello\.py/);
  });

  await t.test('pair mode: Open Diff shows the proposal, Approve applies it in the working tree', async () => {
    const answers = ['Open Diff', 'Approve'];
    mock.onMessage = (message) => (/^Guild wants to/.test(message) ? answers.shift() : undefined);
    const before = webviewOut.length;
    const executedBefore = mock.executed.length;
    await toWebview({ type: 'send', text: 'make hello.py', mode: 'ask' });
    await until('reply', () => webviewOut.slice(before).some((m) => m.type === 'message' && /Created hello\.py/.test(m.text)));
    const diff = mock.executed.slice(executedBefore).find((c) => c[0] === 'vscode.diff');
    assert.ok(diff, 'the diff editor was opened');
    assert.match(diff[1].path, /^\/proposed\/.+\/empty$/); // new file: empty on the left
    assert.strictEqual(mock.docProvider.provideTextDocumentContent(diff[2]), "print('hello')\n");
    assert.strictEqual(fs.readFileSync(path.join(project, 'hello.py'), 'utf8'), "print('hello')\n");
    const reply = webviewOut.slice(before).filter((m) => m.type === 'message').pop();
    assert.match(reply.text, /Changed: hello\.py/);
    assert.strictEqual(git(project, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main'); // no branch, no commit
    assert.match(git(project, 'status', '--porcelain'), /\?\? hello\.py/);
    await until('idle', () => !ctl.busy);
    mock.onMessage = () => undefined;
  });

  await t.test('Stop Current Job cancels a running command without restarting the server', async () => {
    mock.onMessage = (message) => (/^Guild wants to run/.test(message) ? 'Approve' : undefined);
    const proc = ctl.proc;
    const before = webviewOut.length;
    await toWebview({ type: 'send', text: 'do something slow', mode: 'auto-edits' });
    await until('command running', () => mock.channel.lines.some((l) => /approved: run_command/.test(l)));
    const t0 = Date.now();
    await vscode.commands.executeCommand('guild.cancel');
    await until('cancelled', () => webviewOut.slice(before).some((m) => m.type === 'message' && m.text === 'Stopped.'));
    await until('idle', () => !ctl.busy);
    assert.ok(Date.now() - t0 < 15000, 'the 60 s command was killed');
    assert.strictEqual(ctl.proc, proc, 'same server process');
    assert.strictEqual(ctl.status, 'running');
    assert.strictEqual(ctl.state.job.status, 'cancelled');
    mock.onMessage = () => undefined;
  });

  await t.test('Stop ends the server process and the UI falls back to "stopped"', async () => {
    const proc = ctl.proc;
    await vscode.commands.executeCommand('guild.stop');
    assert.strictEqual(ctl.status, 'stopped');
    assert.strictEqual(mock.context['guild.state'], 'stopped');
    assert.ok(proc.exitCode !== null || proc.signalCode !== null, 'the server process ended');
    assert.deepStrictEqual(mock.tree.provider.getChildren(), []);
    await assert.rejects(ctl.run({}), /not running/);
  });
});

test('a folder without guild set up is not started automatically', async () => {
  // a second controller in the same process: only checks the boot decision
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'guild-vsc-empty-'));
  mock.workspaceFolder = dir;
  const { activate, deactivate } = require('../extension');
  const ctl = await activate({ subscriptions: [], extensionUri: vscode.Uri.file(path.join(__dirname, '..')) });
  try {
    assert.strictEqual(ctl.status, 'uninitialised');
    assert.strictEqual(ctl.proc, null);
    assert.ok(!fs.existsSync(path.join(dir, '.guild')), 'nothing was written into the folder');
    await vscode.commands.executeCommand('guild.init');
    assert.deepStrictEqual(mock.terminals[mock.terminals.length - 1].sent, ['guild init']);
  } finally {
    deactivate();
  }
});
