'use strict';
// Guild for VS Code. Plain CommonJS, no dependencies, no build step.
//
// The extension starts `guild ui --no-browser` for the opened folder and drives it through the
// same HTTP API + event stream the web dashboard uses. guild's core stays Python (ADR-001); this
// file is only presentation: a task tree, a chat/activity view, native diff and a status bar item.

const vscode = require('vscode');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { GuildClient } = require('./lib/client');
const server = require('./lib/server');
const fmt = require('./lib/format');

const SCHEME = 'guild';
const enc = encodeURIComponent;

class PlanProvider {
  constructor(ctl) {
    this.ctl = ctl;
    this._changed = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._changed.event;
  }

  refresh() {
    this._changed.fire(undefined);
  }

  getChildren(el) {
    if (!el) {
      const tasks = (this.ctl.state && this.ctl.state.plan && this.ctl.state.plan.tasks) || [];
      return tasks.filter((t) => t && t.id).map((task) => ({ type: 'task', task }));
    }
    if (el.type === 'task') {
      return fmt.taskView(el.task).details.map(([label, value]) => ({ type: 'detail', label, value }));
    }
    return [];
  }

  getTreeItem(el) {
    if (el.type === 'detail') {
      const item = new vscode.TreeItem(`${el.label}: ${fmt.oneLine(el.value, 120)}`);
      item.tooltip = `${el.label}\n\n${el.value}`;
      item.contextValue = 'detail';
      return item;
    }
    const v = fmt.taskView(el.task, this.ctl.busy);
    const item = new vscode.TreeItem(v.label, vscode.TreeItemCollapsibleState.Collapsed);
    item.id = `task:${el.task.id}`;
    item.description = v.description;
    item.iconPath = new vscode.ThemeIcon(v.icon);
    item.contextValue = v.contextValue;
    item.tooltip = [el.task.title, el.task.description, el.task.done_when && `Done when: ${el.task.done_when}`]
      .filter(Boolean)
      .join('\n\n');
    return item;
  }
}

class ChatView {
  constructor(ctl, extensionUri) {
    this.ctl = ctl;
    this.extensionUri = extensionUri;
    this.view = null;
    this.ready = false;
    this.logBuffer = [];
  }

  resolveWebviewView(view) {
    this.view = view;
    this.ready = false;
    const media = vscode.Uri.joinPath(this.extensionUri, 'media');
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = crypto.randomBytes(16).toString('hex');
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'chat.css'));
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'chat.js'));
    view.webview.html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${css}" rel="stylesheet"></head>
<body>
<div id="tabs"><button id="tab-chat">Chat</button><button id="tab-log">Activity</button></div>
<div id="status"></div>
<div id="chat" class="pane"></div>
<div id="log" class="pane"></div>
<pre id="live"></pre>
<div id="composer"><select id="mode" title="Chat only reads. Pair can change files and run commands, asking you first.">
<option value="chat">Chat (read-only)</option>
<option value="ask">Pair: ask before every change</option>
<option value="auto-edits">Pair: apply edits, ask for commands</option>
<option value="plan">Pair: plan only</option>
</select><textarea id="input" rows="1"></textarea><button id="send">Send</button></div>
<script nonce="${nonce}" src="${js}"></script>
</body></html>`;
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    view.onDidDispose(() => {
      this.view = null;
      this.ready = false;
    });
  }

  post(msg) {
    if (this.view && this.ready) this.view.webview.postMessage(msg);
  }

  log(line) {
    this.logBuffer.push(line);
    if (this.logBuffer.length > 600) this.logBuffer.splice(0, this.logBuffer.length - 600);
    this.post({ type: 'log', lines: [line] });
  }

  async onMessage(m) {
    if (m.type === 'ready') {
      this.ready = true;
      this.post({ type: 'log', lines: this.logBuffer });
      this.ctl.pushStatus();
      await this.loadHistory();
      for (const r of this.ctl.approvals.values()) this.post({ type: 'approval', request: r });
    } else if (m.type === 'send') {
      await this.ctl.chat(String(m.text || ''), String(m.mode || 'chat'));
    } else if (m.type === 'approve') {
      await this.ctl.answer(String(m.id), Boolean(m.approve), Boolean(m.remember));
    } else if (m.type === 'viewDiff') {
      const r = this.ctl.approvals.get(String(m.id));
      if (r) await this.ctl.openProposedDiff(r).catch((e) => this.ctl.fail(e));
    }
  }

  async loadHistory() {
    if (!this.ctl.client) return this.post({ type: 'history', items: [] });
    try {
      this.post({ type: 'history', items: await this.ctl.client.get('/api/chat') });
    } catch {
      /* server going away; the next start reloads it */
    }
  }
}

class Controller {
  constructor(context) {
    this.context = context;
    this.state = null; // last GET /api/state
    this.client = null;
    this.proc = null;
    this.url = '';
    this.status = 'stopped';
    this.busy = false;
    this.phase = '';
    this.stopping = false;
    this.approvals = new Map(); // pair mode: id -> request waiting for the owner
    this.docs = new Map(); // virtual documents: uri path -> text
    this._docsChanged = new vscode.EventEmitter();
    this._refreshTimer = null;

    this.channel = vscode.window.createOutputChannel('Guild');
    this.plan = new PlanProvider(this);
    this.tree = vscode.window.createTreeView('guild.plan', { treeDataProvider: this.plan });
    this.chatView = new ChatView(this, context.extensionUri);
    this.bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.bar.command = 'workbench.view.extension.guild';
    this.bar.show();

    context.subscriptions.push(
      this.channel,
      this.tree,
      this.bar,
      vscode.window.registerWebviewViewProvider('guild.chat', this.chatView, {
        webviewOptions: { retainContextWhenHidden: true },
      }),
      vscode.workspace.registerTextDocumentContentProvider(SCHEME, {
        onDidChange: this._docsChanged.event,
        provideTextDocumentContent: (uri) => this.docs.get(uri.path) || '',
      })
    );

    const cmd = (id, fn) =>
      context.subscriptions.push(
        vscode.commands.registerCommand(id, (...args) =>
          Promise.resolve(fn.apply(this, args)).catch((e) => this.fail(e))
        )
      );
    cmd('guild.start', this.start);
    cmd('guild.stop', () => this.stop(true));
    cmd('guild.restart', this.restart);
    cmd('guild.cancel', this.cancelJob);
    cmd('guild.init', this.init);
    cmd('guild.plan', this.newPlan);
    cmd('guild.revisePlan', this.revisePlan);
    cmd('guild.runNext', () => this.run({}));
    cmd('guild.runAll', () => this.run({ all: true }));
    cmd('guild.refresh', this.refresh);
    cmd('guild.runTask', (el) => el && this.run({ task_id: el.task.id }));
    cmd('guild.diffTask', (el) => el && this.diffTask(el.task));
    cmd('guild.mergeTask', (el) => el && this.mergeTask(el.task));
    cmd('guild.discardTask', (el) => el && this.discardTask(el.task));
    cmd('guild.editTask', (el) => el && this.editTask(el.task));
    cmd('guild.deleteTask', (el) => el && this.deleteTask(el.task));
    cmd('guild.markTask', (el) => el && this.markTask(el.task));
    cmd('guild.setProfile', this.setProfile);
    cmd('guild.doctor', this.doctor);
    cmd('guild.showRuns', this.showRuns);
    cmd('guild.showLog', () => this.channel.show(true));
    cmd('guild.openDashboard', () => this.url && vscode.env.openExternal(vscode.Uri.parse(this.url)));
    cmd('guild.openSettings', () =>
      vscode.commands.executeCommand('workbench.action.openSettings', 'guild.command')
    );
    cmd('guild.clearChat', this.clearChat);
  }

  // ------------------------------------------------------------------ lifecycle

  get workspace() {
    const f = vscode.workspace.workspaceFolders;
    return f && f.length ? f[0].uri.fsPath : null;
  }

  cfg(key) {
    return vscode.workspace.getConfiguration('guild').get(key);
  }

  profile() {
    return this.cfg('profile') || undefined;
  }

  setStatus(status) {
    this.status = status;
    vscode.commands.executeCommand('setContext', 'guild.state', status);
    this.pushStatus();
  }

  async boot() {
    const ws = this.workspace;
    if (!ws) return this.setStatus('noFolder');
    const initialised = fs.existsSync(path.join(ws, '.guild', 'config.yaml'));
    if (!initialised) {
      this.setStatus('uninitialised');
      // `guild init` in the terminal creates the config; start as soon as it appears
      const w = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(ws, '.guild/config.yaml')
      );
      w.onDidCreate(() => {
        if (this.status === 'uninitialised') this.start().catch((e) => this.fail(e));
      });
      this.context.subscriptions.push(w);
      return;
    }
    if (this.cfg('autoStart')) await this.start();
    else this.setStatus('stopped');
  }

  async pythonPath() {
    // the Python extension knows which interpreter the user picked for this folder
    try {
      const ext = vscode.extensions.getExtension('ms-python.python');
      if (!ext) return undefined;
      const api = ext.isActive ? ext.exports : await ext.activate();
      const env = api && api.environments && api.environments.getActiveEnvironmentPath();
      const p = env && env.path;
      return p && fs.existsSync(p) && fs.statSync(p).isFile() ? p : undefined;
    } catch {
      return undefined;
    }
  }

  async start() {
    const ws = this.workspace;
    if (!ws) return this.setStatus('noFolder');
    if (this.status === 'starting' || this.status === 'running') return;
    this.setStatus('starting');
    try {
      const srv = await server.start({
        workspace: ws,
        configured: this.cfg('command'),
        python: await this.pythonPath(),
        port: this.cfg('port') || 0,
        log: (line) => this.channel.appendLine(line),
      });
      this.proc = srv.proc;
      this.url = srv.url;
      this.stopping = false;
      srv.proc.on('exit', (code) => this.onExit(srv.proc, code));
      this.client = new GuildClient(srv.url);
      this.client.on('event', (ev, replayed) => this.onEvent(ev, replayed));
      this.client.connectEvents();
      this.channel.appendLine(`guild is running at ${srv.url}`);
      this.setStatus('running');
      await this.refresh();
      await this.chatView.loadHistory();
    } catch (e) {
      this.channel.appendLine(String(e.message || e));
      this.setStatus('error');
      const pick = await vscode.window.showErrorMessage(
        'Guild could not start. Set the path to the guild command in the settings.',
        'Open Settings',
        'Show Log'
      );
      if (pick === 'Open Settings') vscode.commands.executeCommand('guild.openSettings');
      if (pick === 'Show Log') this.channel.show(true);
    }
  }

  onExit(proc, code) {
    if (proc !== this.proc) return;
    this.teardown();
    if (!this.stopping) {
      this.channel.appendLine(`guild server exited (code ${code})`);
      vscode.window.showWarningMessage('The guild server stopped.', 'Start').then((p) => {
        if (p === 'Start') this.start().catch((e) => this.fail(e));
      });
    }
    this.setStatus('stopped');
  }

  teardown() {
    if (this.client) this.client.close();
    this.client = null;
    this.proc = null;
    this.state = null;
    this.busy = false;
    this.phase = '';
    this.url = '';
    this.clearApprovals();
    vscode.commands.executeCommand('setContext', 'guild.hasPlan', false);
    vscode.commands.executeCommand('setContext', 'guild.busy', false);
    this.plan.refresh();
  }

  async stop(announce) {
    const proc = this.proc;
    if (!proc) return this.setStatus(this.workspace ? 'stopped' : 'noFolder');
    this.stopping = true;
    const gone = new Promise((r) => proc.once('exit', r));
    server.stop(proc);
    await Promise.race([gone, new Promise((r) => setTimeout(r, 4000))]);
    if (this.proc === proc) {
      this.teardown();
      this.setStatus('stopped');
    }
    if (announce) this.channel.appendLine('guild server stopped');
  }

  async restart() {
    await this.stop(false);
    await this.start();
  }

  /** Stop the running job. The server stops it at the next safe point; a task goes back to todo. */
  async cancelJob() {
    if (!this.client || !this.busy) return;
    try {
      await this.client.post('/api/cancel', {});
    } catch (e) {
      if (e.status !== 404 && e.status !== 405) throw e;
      await this.restart(); // an older guild without /api/cancel: restarting is the only way
    }
  }

  init() {
    const ws = this.workspace;
    if (!ws) return;
    const term = vscode.window.createTerminal({ name: 'guild init', cwd: ws });
    term.show();
    const argv = server.splitCommand(this.cfg('command'));
    const cmd = (argv.length ? argv : ['guild']).map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(' ');
    term.sendText(`${cmd} init`);
  }

  dispose() {
    this.stopping = true;
    if (this.client) this.client.close();
    server.stop(this.proc);
  }

  // ------------------------------------------------------------------ state

  fail(e) {
    const msg = String((e && e.message) || e).split('\n')[0];
    this.channel.appendLine(`error: ${(e && e.message) || e}`);
    if (e && e.status === 409) vscode.window.showWarningMessage(`Guild is busy: ${msg}`);
    else vscode.window.showErrorMessage(`Guild: ${msg}`);
  }

  need() {
    if (!this.client) throw new Error('guild is not running — use "Guild: Start"');
    return this.client;
  }

  async refresh() {
    if (!this.client) return;
    const state = await this.client.get('/api/state');
    if (!this.client) return; // stopped while the request was in flight
    this.state = state;
    this.busy = Boolean(state.busy);
    if (!this.busy) this.phase = '';
    // approvals that were requested before this window connected (or whose event was missed)
    const pending = state.pending_approvals || [];
    for (const r of pending) this.presentApproval(r);
    for (const id of [...this.approvals.keys()]) {
      if (!pending.some((r) => r.id === id)) this.dropApproval(id);
    }
    const plan = state.plan;
    vscode.commands.executeCommand('setContext', 'guild.hasPlan', Boolean(plan && plan.tasks && plan.tasks.length));
    vscode.commands.executeCommand('setContext', 'guild.busy', this.busy);
    const warnings = state.plan_warnings || [];
    this.tree.message = plan
      ? `${plan.goal || ''}${warnings.length ? `  —  ⚠ ${warnings.length} plan warning(s), hover a task or see the log` : ''}`
      : undefined;
    const key = warnings.join('\n');
    if (key && key !== this._warned) for (const w of warnings) this.channel.appendLine(`plan check: ${w}`);
    this._warned = key;
    this.plan.refresh();
    this.pushStatus();
  }

  refreshSoon() {
    if (this._refreshTimer) return;
    this._refreshTimer = setTimeout(() => {
      this._refreshTimer = null;
      this.refresh().catch(() => {});
    }, 150);
  }

  pushStatus() {
    const running = this.status === 'running';
    let text;
    if (!running) {
      text = { noFolder: 'no folder open', uninitialised: 'not set up in this folder', starting: 'starting…', error: 'could not start', stopped: 'stopped' }[this.status];
    } else if (this.approvals.size) {
      text = 'waiting for your approval';
    } else if (this.busy) {
      text = this.phase || (this.state && this.state.job && this.state.job.kind) || 'working';
    } else {
      const cfg = (this.state && this.state.config) || {};
      text = `idle · profile ${this.cfg('profile') || cfg.profile || '?'}`;
    }
    this.bar.text = running && this.busy ? `$(sync~spin) guild: ${text}` : this.status === 'error' ? '$(warning) guild' : '$(organization) guild';
    this.bar.tooltip = `Guild — ${text}${this.state ? `\n${this.state.root}` : ''}`;
    this.chatView.post({ type: 'status', text, busy: running && this.busy, ready: running });
  }

  onEvent(ev, replayed) {
    const line = fmt.eventLine(ev);
    if (line) {
      this.channel.appendLine(line);
      this.chatView.log(line);
    }
    switch (ev.kind) {
      case 'token':
        if (!replayed) this.chatView.post({ type: 'token', role: ev.role, text: ev.text });
        return;
      case 'model_call':
        this.chatView.post({ type: 'liveEnd' });
        return;
      case 'phase':
        this.phase = `${ev.phase}${ev.task_id ? ' ' + ev.task_id : ''}`;
        if (!replayed) this.busy = true;
        this.pushStatus();
        this.refreshSoon();
        return;
      case 'task_end':
        this.refreshSoon();
        if (!replayed) this.announceTask(ev);
        return;
      case 'job_done':
        this.refreshSoon();
        if (replayed) return;
        if (ev.job === 'chat' || ev.job === 'pair') {
          const r = ev.result || {};
          const files = (r.files_changed || []).length ? `\n\nChanged: ${r.files_changed.join(', ')}` : '';
          this.chatView.post({ type: 'message', role: r.model ? `assistant · ${r.model}` : 'assistant', text: (r.text || '') + files, cls: 'assistant' });
        } else if (ev.job === 'plan' || ev.job === 'revise') {
          const n = ev.result && ev.result.tasks ? ev.result.tasks.length : 0;
          this.notify('info', `Guild: plan ready — ${n} task(s).`);
        } else if (ev.job === 'ask') {
          const r = ev.result || {};
          this.showDoc(`ask/${Date.now()}.json`, r.data ? JSON.stringify(r.data, null, 2) : r.raw || '');
        }
        return;
      case 'job_error':
        this.refreshSoon();
        if (replayed) return;
        if (ev.job === 'chat' || ev.job === 'pair') {
          this.chatView.post({ type: 'message', role: 'guild', text: String(ev.error || 'failed'), cls: 'error' });
        }
        this.notify('error', `Guild: ${ev.job} failed — ${String(ev.error || '').split('\n')[0]}`, 'Show Log').then((p) => {
          if (p === 'Show Log') this.channel.show(true);
        });
        return;
      case 'approval_request':
        if (!replayed && ev.request) this.presentApproval(ev.request);
        return;
      case 'approval_resolved':
        this.dropApproval(ev.id);
        return;
      case 'job_cancelled':
        this.clearApprovals();
        this.refreshSoon();
        if (!replayed && (ev.job === 'chat' || ev.job === 'pair')) {
          this.chatView.post({ type: 'message', role: 'guild', text: 'Stopped.', cls: 'error' });
        }
        return;
      case 'decision':
      case 'project_switched':
        this.refreshSoon();
        return;
      default:
    }
  }

  notify(level, message, ...actions) {
    if (!this.cfg('notifications')) return Promise.resolve(undefined);
    const fn = { info: 'showInformationMessage', warn: 'showWarningMessage', error: 'showErrorMessage' }[level];
    return Promise.resolve(vscode.window[fn](message, ...actions));
  }

  async announceTask(ev) {
    if (ev.accepted) {
      const actions = ev.branch ? ['Review Changes', 'Merge'] : [];
      const pick = await this.notify('info', `Guild: ${ev.task_id} accepted after ${ev.rounds} round(s).`, ...actions);
      const task = this.findTask(ev.task_id) || { id: ev.task_id, branch: ev.branch };
      if (pick === 'Review Changes') this.diffTask(task).catch((e) => this.fail(e));
      if (pick === 'Merge') this.mergeTask(task).catch((e) => this.fail(e));
    } else {
      const pick = await this.notify('warn', `Guild: ${ev.task_id} was not accepted (${ev.rounds} round(s)).`, 'Show Log');
      if (pick === 'Show Log') this.channel.show(true);
    }
  }

  findTask(id) {
    const tasks = (this.state && this.state.plan && this.state.plan.tasks) || [];
    return tasks.find((t) => t.id === id);
  }

  // ------------------------------------------------------------------ jobs

  async job(apiPath, body) {
    await this.need().post(apiPath, body);
    this.busy = true;
    vscode.commands.executeCommand('setContext', 'guild.busy', true);
    this.pushStatus();
  }

  async newPlan() {
    this.need();
    const goal = await vscode.window.showInputBox({
      title: 'Guild: new plan',
      prompt: 'What do you want built? The Lead turns this into a roadmap and tasks.',
      placeHolder: 'Add a multiply(a, b) function to app.py and a test for it',
      ignoreFocusOut: true,
    });
    if (!goal || !goal.trim()) return;
    if (this.state && this.state.plan && this.state.plan.tasks && this.state.plan.tasks.length) {
      const ok = await vscode.window.showWarningMessage(
        'This replaces the current plan and its task list.',
        { modal: true },
        'Replace Plan'
      );
      if (ok !== 'Replace Plan') return;
    }
    await this.job('/api/plan', { goal: goal.trim(), profile: this.profile() });
    this.chatView.post({ type: 'tab', tab: 'log' });
  }

  async revisePlan() {
    this.need();
    const instruction = await vscode.window.showInputBox({
      title: 'Guild: revise plan',
      prompt: 'Tell the Lead what to change. Finished tasks keep their status.',
      placeHolder: 'Split T2 into two tasks and add input validation',
      ignoreFocusOut: true,
    });
    if (!instruction || !instruction.trim()) return;
    await this.job('/api/plan/revise', { instruction: instruction.trim(), profile: this.profile() });
    this.chatView.post({ type: 'tab', tab: 'log' });
  }

  async run(body) {
    await this.job('/api/run', { ...body, profile: this.profile() });
    this.chatView.post({ type: 'tab', tab: 'log' });
  }

  async chat(text, mode = 'chat') {
    if (!text.trim()) return;
    try {
      if (mode === 'chat') await this.need().post('/api/chat', { message: text, profile: this.profile() });
      else await this.need().post('/api/pair', { message: text, mode, profile: this.profile() });
      this.chatView.post({ type: 'message', role: 'user', text, cls: 'user' });
      this.busy = true;
      this.pushStatus();
    } catch (e) {
      this.chatView.post({ type: 'message', role: 'guild', text: `not sent: ${e.message}`, cls: 'error' });
    }
  }

  async clearChat() {
    if (this.client) await this.client.delete('/api/chat');
    this.chatView.post({ type: 'history', items: [] });
  }

  // ------------------------------------------------------------------ pair mode approvals

  /** Show a change the agent wants to make: a card in the chat view and a notification. */
  presentApproval(req) {
    if (!req || !req.id || this.approvals.has(req.id)) return;
    this.approvals.set(req.id, req);
    this.chatView.post({ type: 'approval', request: req });
    this.pushStatus();
    this.promptApproval(req, true).catch((e) => this.fail(e));
  }

  async promptApproval(req, offerDiff) {
    const actions = ['Approve', ...(req.kind === 'edit' && offerDiff ? ['Open Diff'] : []), 'Decline'];
    // always shown: unlike "task finished", this one blocks the agent until it is answered
    const pick = await vscode.window.showInformationMessage(`Guild wants to ${fmt.approvalTitle(req)}`, ...actions);
    if (!this.approvals.has(req.id)) return; // answered in the chat view or cancelled meanwhile
    if (pick === 'Open Diff') {
      await this.openProposedDiff(req);
      return this.promptApproval(req, false);
    }
    if (pick === 'Approve') await this.answer(req.id, true, false);
    if (pick === 'Decline') await this.answer(req.id, false, false);
    // dismissed: the card in the chat view is still there
  }

  async answer(id, approve, remember) {
    if (!this.approvals.has(id) || !this.client) return;
    try {
      await this.client.post(`/api/approve/${enc(id)}`, { approve, remember });
    } catch (e) {
      if (e.status !== 404) throw e; // 404: already answered elsewhere (dashboard) or cancelled
    }
    this.dropApproval(id);
  }

  dropApproval(id) {
    if (!this.approvals.delete(id)) return;
    this.chatView.post({ type: 'approvalDone', id });
    this.pushStatus();
  }

  clearApprovals() {
    for (const id of [...this.approvals.keys()]) this.dropApproval(id);
  }

  /** VS Code's diff editor: the file as it is now (left) against what the agent proposes. */
  async openProposedDiff(req) {
    if (req.kind !== 'edit') return;
    const root = (this.state && this.state.root) || this.workspace;
    const empty = vscode.Uri.from({ scheme: SCHEME, path: `/proposed/${req.id}/empty` });
    const right = vscode.Uri.from({ scheme: SCHEME, path: `/proposed/${req.id}/${req.path}` });
    this.docs.set(empty.path, '');
    this.docs.set(right.path, typeof req.proposed === 'string' ? req.proposed : `(file too large to preview)\n\n${req.preview}`);
    this._docsChanged.fire(right);
    const left = req.new_file ? empty : vscode.Uri.file(path.join(root, req.path));
    await vscode.commands.executeCommand('vscode.diff', left, right, `${req.path} (now ↔ proposed by guild)`, { preview: true });
  }

  // ------------------------------------------------------------------ plan editing

  async postTask(body) {
    await this.need().post('/api/plan/task', body);
    await this.refresh();
  }

  async editTask(task) {
    const ask = (title, value) => vscode.window.showInputBox({ title: `${task.id}: ${title}`, value: value || '', ignoreFocusOut: true });
    const title = await ask('title', task.title);
    if (title === undefined) return;
    const description = await ask('description', task.description);
    if (description === undefined) return;
    const done_when = await ask('done when (a check guild can run, e.g. "test_multiply passes")', task.done_when);
    if (done_when === undefined) return;
    await this.postTask({ id: task.id, title, description, done_when });
  }

  async markTask(task) {
    const pick = await vscode.window.showQuickPick(['todo', 'done', 'blocked'], { title: `${task.id}: set status` });
    if (pick) await this.postTask({ id: task.id, status: pick });
  }

  async deleteTask(task) {
    const ok = await vscode.window.showWarningMessage(`Delete task ${task.id} from the plan?`, { modal: true }, 'Delete');
    if (ok === 'Delete') await this.postTask({ id: task.id, delete: true });
  }

  // ------------------------------------------------------------------ branches

  async diffTask(task) {
    const d = await this.need().get(`/api/branches/${enc(task.id)}/diff`);
    if (!d.exists) {
      vscode.window.showInformationMessage(`Guild: branch ${d.branch} no longer exists.`);
      return;
    }
    const files = fmt.diffFiles(d.diff);
    if (!files.length) {
      vscode.window.showInformationMessage(`Guild: ${d.branch} has no changes against ${d.base}.`);
      return;
    }
    const WHOLE = `$(diff) Whole change as one diff (${files.length} file(s))`;
    const icon = { added: '$(diff-added)', deleted: '$(diff-removed)', modified: '$(diff-modified)' };
    const pick = await vscode.window.showQuickPick(
      [{ label: WHOLE, description: `${d.branch} vs ${d.base}` }, ...files.map((f) => ({ label: `${icon[f.status]} ${f.path}`, description: f.status, file: f }))],
      { title: `${task.id}: review changes`, placeHolder: 'Pick a file to compare side by side' }
    );
    if (!pick) return;
    if (!pick.file || !(await this.openFileDiff(d, pick.file, task))) {
      await this.showDoc(`diff/${task.id}.diff`, `# ${d.branch} vs ${d.base}\n# ${d.commits || ''}\n\n${d.stat}\n\n${d.diff}\n`);
    }
  }

  /** Side-by-side diff of one file using the built-in Git extension. False if unavailable. */
  async openFileDiff(d, file, task) {
    try {
      const ext = vscode.extensions.getExtension('vscode.git');
      if (!ext) return false;
      const git = (ext.isActive ? ext.exports : await ext.activate()).getAPI(1);
      const root = (this.state && this.state.root) || this.workspace;
      const uri = vscode.Uri.file(path.join(root, file.path));
      await vscode.commands.executeCommand(
        'vscode.diff',
        git.toGitUri(uri, d.base),
        git.toGitUri(uri, d.branch),
        `${file.path} (${d.base} ↔ ${task.id})`,
        { preview: true }
      );
      return true;
    } catch (e) {
      this.channel.appendLine(`side-by-side diff unavailable (${e.message}); showing the unified diff`);
      return false;
    }
  }

  async mergeTask(task) {
    const base = task.base || 'the default branch';
    const ok = await vscode.window.showWarningMessage(
      `Merge ${task.branch || task.id} into ${base}?`,
      { modal: true, detail: 'guild checks out the target branch and merges with --no-ff. It refuses on uncommitted changes or conflicts.' },
      'Merge'
    );
    if (ok !== 'Merge') return;
    const res = await this.need().post(`/api/branches/${enc(task.id)}/merge`, {});
    if (res.ok) vscode.window.showInformationMessage(`Guild: merged ${res.merged} into ${res.base}.`);
    else vscode.window.showErrorMessage(`Guild: merge failed — ${String(res.error || '').split('\n')[0]}`);
    if (!res.ok) this.channel.appendLine(`merge failed: ${res.error}`);
    await this.refresh();
  }

  async discardTask(task) {
    const ok = await vscode.window.showWarningMessage(
      `Delete branch ${task.branch}?`,
      { modal: true, detail: 'Its changes are lost and the task goes back to "todo".' },
      'Discard Branch'
    );
    if (ok !== 'Discard Branch') return;
    const res = await this.need().post(`/api/branches/${enc(task.id)}/discard`, {});
    if (!res.ok) vscode.window.showErrorMessage(`Guild: ${String(res.output || 'could not delete the branch').split('\n')[0]}`);
    await this.refresh();
  }

  // ------------------------------------------------------------------ misc

  async showDoc(name, text) {
    const uri = vscode.Uri.from({ scheme: SCHEME, path: '/' + name });
    this.docs.set(uri.path, text);
    this._docsChanged.fire(uri);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  async setProfile() {
    const profiles = (this.state && this.state.profiles) || [];
    const current = this.state && this.state.config && this.state.config.profile;
    const pick = await vscode.window.showQuickPick(
      profiles.map((p) => ({ label: p, description: p === current ? 'current' : '' })),
      { title: 'Guild: profile for this project' }
    );
    if (!pick) return;
    await this.need().post('/api/config', { profile: pick.label });
    await this.refresh();
  }

  async doctor() {
    const p = this.profile();
    const d = await this.need().get('/api/doctor' + (p ? `?profile=${enc(p)}` : ''));
    this.channel.appendLine('');
    this.channel.appendLine(fmt.doctorText(d));
    this.channel.show(true);
  }

  async showRuns() {
    const runs = await this.need().get('/api/runs');
    if (!runs.length) {
      vscode.window.showInformationMessage('Guild: no runs yet.');
      return;
    }
    const pick = await vscode.window.showQuickPick(
      runs.map((r) => ({
        label: r.id,
        description: `${r.profile || '?'} · ${r.calls} calls · ${r.input_tokens + r.output_tokens} tok · $${r.usd}`,
        detail: (r.phases || []).join(' → '),
      })),
      { title: 'Guild: runs', matchOnDescription: true }
    );
    if (!pick) return;
    const detail = await this.need().get(`/api/runs/${enc(pick.label)}`);
    await this.showDoc(`runs/${pick.label}.log`, fmt.traceText(pick.label, detail));
  }
}

let controller = null;

async function activate(context) {
  controller = new Controller(context);
  context.subscriptions.push({ dispose: () => controller && controller.dispose() });
  await controller.boot();
  return controller; // exposed for tests
}

function deactivate() {
  if (controller) controller.dispose();
  controller = null;
}

module.exports = { activate, deactivate };
