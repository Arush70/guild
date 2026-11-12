// Webview side of the "Chat & Activity" view. It never talks to the guild server itself:
// everything goes through the extension (postMessage), which owns the HTTP connection.
(function () {
  const vscode = acquireVsCodeApi();
  const $ = (id) => document.getElementById(id);
  const chat = $('chat'), log = $('log'), live = $('live'), input = $('input'), send = $('send'), status = $('status'), mode = $('mode');
  let busy = false, ready = false, liveRole = '';

  function atBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }
  function scroll(el, force) { if (force) el.scrollTop = el.scrollHeight; }

  function addMessage(role, text, cls) {
    const empty = chat.querySelector('.empty'); if (empty) empty.remove();
    const stick = atBottom(chat);
    const m = document.createElement('div'); m.className = 'msg ' + (cls || role);
    const who = document.createElement('div'); who.className = 'who'; who.textContent = role === 'user' ? 'you' : role;
    const body = document.createElement('div'); body.className = 'body'; body.textContent = text;
    m.append(who, body); chat.append(m); scroll(chat, stick || role === 'user');
  }

  function renderHistory(items) {
    chat.textContent = '';
    if (!items.length) {
      const e = document.createElement('div'); e.className = 'empty';
      e.textContent = 'Ask about this project: the assistant reads your files before answering. Activity from plans and runs shows under "Activity".';
      chat.append(e); return;
    }
    for (const h of items) addMessage(h.role === 'user' ? 'user' : (h.model ? 'assistant · ' + h.model : 'assistant'), h.content, h.role);
  }

  function addLog({ text, cls }) {
    const stick = atBottom(log);
    const line = document.createElement('div'); line.textContent = text;
    if (cls) line.className = cls;
    log.append(line);
    while (log.childElementCount > 1500) log.firstElementChild.remove();
    scroll(log, stick);
  }

  function setLive(role, text) {
    if (!text) { live.textContent = ''; live.classList.remove('on'); liveRole = ''; return; }
    if (role !== liveRole) { live.textContent = role + ' ▸ '; liveRole = role; }
    live.textContent += text;
    if (live.textContent.length > 6000) live.textContent = role + ' ▸ …' + live.textContent.slice(-4000);
    live.classList.add('on'); live.scrollTop = live.scrollHeight;
  }

  function update() {
    send.disabled = busy || !ready;
    const ask = mode.value === 'chat' ? 'Ask about this project' : mode.value === 'plan' ? 'Ask for a plan (nothing is changed)' : 'Say what to change';
    input.placeholder = !ready ? 'guild is not running' : busy ? 'guild is busy…' : ask + ' (Enter to send, Shift+Enter for a new line)';
  }

  // Pair mode: a change the agent wants to make, waiting for the owner.
  function addApproval(r) {
    if (document.getElementById('ap-' + r.id)) return;
    const empty = chat.querySelector('.empty'); if (empty) empty.remove();
    const card = document.createElement('div'); card.className = 'approval'; card.id = 'ap-' + r.id;
    const what = document.createElement('div'); what.className = 'what';
    what.textContent = r.kind === 'edit' ? (r.new_file ? 'Create ' : 'Edit ') + r.path + '?' : 'Run this command?';
    const pre = document.createElement('pre');
    for (const line of String(r.kind === 'edit' ? r.preview : r.command).split('\n')) {
      const s = document.createElement('span'); s.textContent = line + '\n';
      if (r.kind === 'edit' && /^\+(?!\+\+)/.test(line)) s.className = 'add';
      if (r.kind === 'edit' && /^-(?!--)/.test(line)) s.className = 'del';
      pre.append(s);
    }
    const acts = document.createElement('div'); acts.className = 'acts';
    const btn = (label, msg, cls) => { const b = document.createElement('button'); b.textContent = label; if (cls) b.className = cls; b.onclick = () => vscode.postMessage(msg); acts.append(b); };
    btn('Approve', { type: 'approve', id: r.id, approve: true }, 'primary');
    if (r.kind === 'edit') {
      btn('Approve all edits this turn', { type: 'approve', id: r.id, approve: true, remember: true });
      btn('Open diff', { type: 'viewDiff', id: r.id });
    }
    btn('Decline', { type: 'approve', id: r.id, approve: false });
    card.append(what, pre, acts); chat.append(card); show('chat'); chat.scrollTop = chat.scrollHeight;
  }

  function show(tab) {
    for (const t of ['chat', 'log']) { $(t).classList.toggle('on', t === tab); $('tab-' + t).classList.toggle('on', t === tab); }
    vscode.setState({ tab, mode: mode.value });
  }
  mode.onchange = () => { vscode.setState({ ...(vscode.getState() || {}), mode: mode.value }); update(); };
  $('tab-chat').onclick = () => show('chat');
  $('tab-log').onclick = () => show('log');

  function submit() {
    const text = input.value.trim();
    if (!text || busy || !ready) return;
    input.value = ''; show('chat');
    vscode.postMessage({ type: 'send', text, mode: mode.value });
  }
  send.onclick = submit;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });

  window.addEventListener('message', (e) => {
    const m = e.data;
    switch (m.type) {
      case 'history': renderHistory(m.items || []); break;
      case 'message': addMessage(m.role, m.text, m.cls); break;
      case 'log': for (const l of m.lines) addLog(l); break;
      case 'token': setLive(m.role, m.text); break;
      case 'liveEnd': setLive('', ''); break;
      case 'status': busy = m.busy; ready = m.ready; status.textContent = m.text; status.classList.toggle('busy', m.busy); update(); if (!m.busy) setLive('', ''); break;
      case 'tab': show(m.tab); break;
      case 'approval': addApproval(m.request); break;
      case 'approvalDone': { const c = document.getElementById('ap-' + m.id); if (c) c.remove(); break; }
    }
  });

  mode.value = (vscode.getState() || {}).mode || 'chat';
  show((vscode.getState() || {}).tab || 'chat');
  update();
  vscode.postMessage({ type: 'ready' });
})();
