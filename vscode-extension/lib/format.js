'use strict';
// Pure helpers: event -> log line, task presentation, diff parsing. No `vscode` import.

function clock(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toTimeString().slice(0, 8);
}

function oneLine(s, n = 160) {
  return String(s ?? '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, n);
}

/**
 * One log line for an event from /api/events: {text, cls}, or null for events that are not
 * logged. cls says what the line means (phase | ok | bad | warn | '') so a view can colour it
 * without reading the text back.
 */
function eventLine(ev) {
  const t = clock(ev.ts);
  const line = (text, cls = '') => ({ text: `${t} ${text}`, cls });
  switch (ev.kind) {
    case 'phase':
      return line(`▶ ${ev.phase}${ev.task_id ? ' ' + ev.task_id : ''}${ev.round ? ' (round ' + ev.round + ')' : ''}`, 'phase');
    case 'model_call':
      return line(
        `  ${ev.role}: ${ev.model} · ${ev.input_tokens}→${ev.output_tokens} tok · ${ev.latency_s}s` +
          (ev.usd ? ` · $${ev.usd}` : '') +
          (ev.n_tool_calls ? ` · ${ev.n_tool_calls} tool call(s)` : '')
      );
    case 'tool_call': {
      const args = Object.entries(ev.args || {})
        .map(([k, v]) => `${k}=${oneLine(JSON.stringify(v), 40)}`)
        .join(', ');
      return line(
        `    ${ev.ok ? '·' : '✖'} ${ev.role} ${ev.tool}(${args}) → ${oneLine(ev.result_preview, 100)}`,
        ev.ok ? '' : 'bad'
      );
    }
    case 'verification':
      return line(
        `  tests: ${ev.passed ? 'PASS' : 'FAIL'} (${ev.tests_run} run)` +
          (ev.failures && ev.failures.length ? ' — ' + oneLine(ev.failures.slice(0, 3).join('; '), 200) : ''),
        ev.passed ? 'ok' : 'bad'
      );
    case 'review_input':
      return line(`  reviewers see ${ev.diff_chars} chars of diff: ${(ev.files_in_diff || []).join(', ') || '(no files)'}`);
    case 'decision':
      return line(`  ${ev.role}: ${ev.decision || ev.status || ''} ${oneLine(ev.notes || ev.summary || '')}`.trimEnd());
    case 'fallback':
      return line(`! fallback: ${ev.model} failed (${oneLine(ev.reason)})`, 'warn');
    case 'note':
      return line(`  ${ev.role || 'guild'}: ${oneLine(ev.msg, 300)}`);
    case 'report':
      // "phase" and "fallback" reports are terminal text for events that are logged themselves
      if (ev.level === 'phase' || ev.level === 'fallback') return null;
      return ev.level === 'warn' ? line(`! ${oneLine(ev.msg, 400)}`, 'warn') : line(`· ${oneLine(ev.msg, 400)}`);
    case 'task_end':
      return line(
        `${ev.accepted ? '✔' : '✖'} task ${ev.task_id} ${ev.accepted ? 'ACCEPTED' : 'not accepted'}` +
          ` · ${ev.rounds} round(s) · $${ev.cost_usd}${ev.branch ? ' · branch ' + ev.branch : ''}`,
        ev.accepted ? 'ok' : 'bad'
      );
    case 'error':
      return line(`✖ ${ev.task_id ? ev.task_id + ': ' : ''}${oneLine(ev.error, 400)}`, 'bad');
    case 'run_end':
      return line(`■ run finished · $${ev.total_usd}`);
    case 'job_done':
      return line(`✔ ${ev.job} finished`, 'ok');
    case 'job_error':
      return line(`✖ ${ev.job} failed: ${String(ev.error || '').trim()}`, 'bad');
    case 'job_cancelled':
      return line(`■ ${ev.job} cancelled`, 'warn');
    case 'approval_request':
      return line(`? waiting for approval: ${approvalTitle(ev.request || {})}`, 'warn');
    case 'approval':
      return line(
        `  ${ev.approved ? 'approved' : 'declined'}${ev.auto ? ' (auto)' : ''}: ${ev.tool} ${oneLine(ev.target, 120)}`,
        ev.approved ? '' : 'warn'
      );
    case 'project_switched':
      return line(`· project: ${ev.root}`);
    default:
      return null; // token, run_start, unknown kinds
  }
}

const STATUS_ICON = {
  todo: 'circle-large-outline',
  in_progress: 'sync~spin',
  done: 'pass',
  blocked: 'error',
};

/** Presentation of a plan task for the tree. */
function taskView(task, busy = true) {
  let status = STATUS_ICON[task.status] ? task.status : 'todo';
  // "in_progress" while nothing is running is a leftover from a stopped run; guild will pick
  // the task up again, so show it as waiting rather than spinning forever
  const interrupted = status === 'in_progress' && !busy;
  if (interrupted) status = 'todo';
  const flags = [status];
  if (task.branch) flags.push(task.merged ? 'merged' : 'hasBranch');
  const bits = [interrupted ? 'todo (interrupted)' : status.replace('_', ' ')];
  if (task.merged) bits.push('merged');
  else if (task.branch) bits.push('branch ready');
  if (Array.isArray(task.depends_on) && task.depends_on.length) {
    bits.push('after ' + task.depends_on.join(', '));
  }
  return {
    label: `${task.id}  ${task.title || ''}`.trim(),
    description: bits.join(' · '),
    icon: task.merged ? 'git-merge' : STATUS_ICON[status],
    contextValue: 'task ' + flags.join(' '),
    details: [
      ['Description', task.description],
      ['Done when', task.done_when],
      ['Files', (task.files || []).join(', ')],
      ['Branch', task.branch ? `${task.branch}${task.base ? ' (from ' + task.base + ')' : ''}` : ''],
      ['Notes', task.notes],
    ].filter(([, v]) => v && String(v).trim()),
  };
}

/** Short description of a pair-mode approval request. */
function approvalTitle(r) {
  if (r.kind === 'edit') return `${r.new_file ? 'create' : 'edit'} ${r.path}`;
  return `run: ${oneLine(r.command, 120)}`;
}

/** Files touched by a unified diff: [{path, status}] with status added|deleted|modified. */
function diffFiles(diff) {
  const files = [];
  let cur = null;
  for (const line of String(diff || '').split('\n')) {
    const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (m) {
      cur = { path: m[2], status: 'modified' };
      files.push(cur);
    } else if (cur && /^new file/.test(line)) {
      cur.status = 'added';
    } else if (cur && /^deleted file/.test(line)) {
      cur.status = 'deleted';
    }
  }
  return files;
}

/** Plain-text rendering of a run's trace (GET /api/runs/<id>). */
function traceText(runId, detail) {
  const lines = [`guild run ${runId}`, ''];
  for (const [role, v] of Object.entries(detail.by_role || {})) {
    lines.push(
      `${role.padEnd(10)} ${String(v.calls).padStart(3)} calls  ${v.input_tokens}→${v.output_tokens} tok  ` +
        `${v.latency_s}s  $${v.usd}  ${(v.models || []).join(', ')}`
    );
  }
  lines.push('');
  for (const ev of detail.events || []) {
    const line = eventLine(ev);
    if (line) lines.push(line.text);
  }
  return lines.join('\n') + '\n';
}

function doctorText(d) {
  const p = d.profile || {};
  const lines = [`profile: ${p.name} — ${p.description || ''}`, `git: ${d.git ? 'yes' : 'NO (no branches, no merge)'}`, ''];
  let last = '';
  for (const r of d.rows || []) {
    lines.push(`${(r.slot === last ? '' : r.slot).padEnd(18)} ${r.ok ? '✔' : '✖'} ${r.model.padEnd(40)} ${r.status}`);
    last = r.slot;
  }
  if ((d.ollama_models || []).length) lines.push('', 'ollama models: ' + d.ollama_models.join(', '));
  return lines.join('\n');
}

module.exports = { eventLine, taskView, diffFiles, traceText, doctorText, oneLine, approvalTitle };
