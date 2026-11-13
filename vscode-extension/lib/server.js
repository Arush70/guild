'use strict';
// Finds the `codeguild` command and runs `codeguild ui --no-browser` as a child process.
// No `vscode` import: testable with plain node.

const cp = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const IS_WIN = process.platform === 'win32';

/** Split a command line into argv, honouring "double" and 'single' quotes. */
function splitCommand(line) {
  const out = [];
  const rx = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = rx.exec(String(line || '')))) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

/**
 * Ways to launch codeguild, best first. Each is argv (command + leading args).
 * @param {{workspace: string, configured?: string, python?: string}} o
 */
function candidates(o) {
  const list = [];
  const configured = splitCommand(o.configured);
  if (configured.length) list.push(configured);
  for (const venv of ['.venv', 'venv', 'env']) {
    const exe = IS_WIN
      ? path.join(o.workspace, venv, 'Scripts', 'codeguild.exe')
      : path.join(o.workspace, venv, 'bin', 'codeguild');
    if (fs.existsSync(exe)) list.push([exe]);
  }
  if (o.python) list.push([o.python, '-m', 'codeguild.cli']);
  list.push(['codeguild']);
  return list;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function ping(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/api/project', timeout: 1500 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

/**
 * Start `codeguild ui` for a folder and wait until it answers.
 * @returns {Promise<{proc: import('child_process').ChildProcess, url: string, port: number, argv: string[]}>}
 */
async function start({ workspace, configured, python, port, log = () => {}, timeoutMs = 45000 }) {
  const tried = [];
  for (const argv of candidates({ workspace, configured, python })) {
    const p = port || (await freePort());
    const args = [...argv.slice(1), 'ui', '--no-browser', '--port', String(p), '-C', workspace];
    log(`starting: ${argv[0]} ${args.join(' ')}`);
    let proc;
    let spawnError = null;
    let exited = null;
    let output = '';
    try {
      proc = cp.spawn(argv[0], args, {
        cwd: workspace,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
      });
    } catch (e) {
      tried.push(`${argv.join(' ')}: ${e.message}`);
      continue;
    }
    proc.on('error', (e) => (spawnError = e));
    proc.on('exit', (code) => (exited = code ?? -1));
    const onData = (d) => {
      const s = d.toString('utf8');
      output = (output + s).slice(-4000);
      for (const line of s.split(/\r?\n/)) if (line.trim()) log(`[codeguild ui] ${line}`);
    };
    proc.stdout.on('data', onData);
    proc.stderr.on('data', onData);

    const deadline = Date.now() + timeoutMs;
    let ready = false;
    while (Date.now() < deadline && !spawnError && exited === null) {
      if (await ping(p)) {
        ready = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 250));
    }
    if (ready) return { proc, url: `http://127.0.0.1:${p}`, port: p, argv };
    stop(proc);
    const why = spawnError
      ? spawnError.code === 'ENOENT'
        ? 'command not found'
        : spawnError.message
      : exited !== null
        ? `exited with code ${exited}${output ? ': ' + lastLine(output) : ''}`
        : 'did not answer in time';
    tried.push(`${argv.join(' ')}: ${why}`);
  }
  const err = new Error(`could not start codeguild.\n${tried.map((t) => '  - ' + t).join('\n')}`);
  err.tried = tried;
  throw err;
}

function lastLine(text) {
  const lines = text.trim().split(/\r?\n/);
  return lines[lines.length - 1].slice(0, 300);
}

/** Stop the server and everything it started (the test command may be running under it). */
function stop(proc) {
  if (!proc || proc.exitCode !== null || proc.killed) return;
  try {
    if (IS_WIN) {
      cp.spawnSync('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true });
    } else {
      proc.kill('SIGTERM');
      setTimeout(() => {
        if (proc.exitCode === null) proc.kill('SIGKILL');
      }, 2000).unref();
    }
  } catch {
    /* already gone */
  }
}

module.exports = { start, stop, candidates, splitCommand, freePort };
