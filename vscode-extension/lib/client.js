'use strict';
// HTTP + Server-Sent-Events client for the codeguild dashboard API (`codeguild ui`).
// Runs in the extension host (Node). No `vscode` import here so it can be tested with plain node.
//
// All requests are made from Node on purpose: the server refuses state-changing requests that
// carry a foreign Origin header, which is what a webview would send.

const http = require('http');
const { EventEmitter } = require('events');

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

class GuildClient extends EventEmitter {
  /** @param {string} baseUrl e.g. http://127.0.0.1:7331 */
  constructor(baseUrl) {
    super();
    this.base = new URL(baseUrl);
    this._closed = false;
    this._req = null;
    this._timer = null;
    this._lastTs = 0;
    this._atLastTs = new Set();
    this._connectedOnce = false;
  }

  request(method, path, body, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
      const req = http.request(
        {
          host: this.base.hostname,
          port: this.base.port,
          path,
          method,
          headers: data
            ? { 'content-type': 'application/json', 'content-length': data.length }
            : {},
          timeout: timeoutMs,
        },
        (res) => {
          const chunks = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let json;
            try {
              json = text ? JSON.parse(text) : null;
            } catch {
              json = undefined;
            }
            if (res.statusCode >= 200 && res.statusCode < 300) {
              resolve(json === undefined ? text : json);
            } else {
              const detail = json && typeof json === 'object' && json.detail;
              const msg = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : text;
              reject(new ApiError(res.statusCode, (msg || `HTTP ${res.statusCode}`).slice(0, 500)));
            }
          });
        }
      );
      req.on('timeout', () => req.destroy(new Error(`timed out: ${method} ${path}`)));
      req.on('error', reject);
      if (data) req.write(data);
      req.end();
    });
  }

  get(path) {
    return this.request('GET', path);
  }

  post(path, body = {}) {
    return this.request('POST', path, body);
  }

  delete(path) {
    return this.request('DELETE', path);
  }

  /** Open the event stream; emits 'event' (ev, replayed), 'connected', 'disconnected'. Reconnects. */
  connectEvents() {
    if (this._closed) return;
    const startedAt = Date.now() / 1000;
    const req = http.get(
      { host: this.base.hostname, port: this.base.port, path: '/api/events' },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          this._retry();
          return;
        }
        this.emit('connected');
        const first = !this._connectedOnce;
        this._connectedOnce = true;
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          buf += chunk;
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            for (const ev of parseSseBlock(block)) this._deliver(ev, startedAt, first);
          }
        });
        res.on('end', () => this._retry());
        res.on('error', () => this._retry());
      }
    );
    req.on('error', () => this._retry());
    this._req = req;
  }

  _deliver(ev, startedAt, first) {
    const ts = typeof ev.ts === 'number' ? ev.ts : 0;
    // The server replays its recent history to every new subscriber. After a reconnect that
    // history was already seen: drop it. On the first connect keep it, flagged as replayed so
    // nobody pops up a notification for a job that failed an hour ago.
    const old = ts && ts < startedAt - 1;
    if (ev.kind !== 'token' && ts) {
      // Two events can carry the same timestamp, so "not newer" is not enough to call one
      // a repeat: at the newest timestamp, remember exactly which events were delivered.
      const key = JSON.stringify(ev);
      if (ts > this._lastTs) {
        this._lastTs = ts;
        this._atLastTs = new Set([key]);
      } else if (!first && (ts < this._lastTs || this._atLastTs.has(key))) {
        return;
      } else if (ts === this._lastTs) {
        this._atLastTs.add(key);
      }
    }
    this.emit('event', ev, Boolean(old));
  }

  _retry() {
    if (this._closed || this._timer) return;
    this.emit('disconnected');
    this._timer = setTimeout(() => {
      this._timer = null;
      this.connectEvents();
    }, 1500);
  }

  close() {
    this._closed = true;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (this._req) this._req.destroy();
    this.removeAllListeners();
  }
}

/** Parse one SSE block ("data: {...}" lines; comments and retry: are ignored). */
function parseSseBlock(block) {
  const out = [];
  const data = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  if (data.length) {
    try {
      const ev = JSON.parse(data.join('\n'));
      if (ev && typeof ev === 'object') out.push(ev);
    } catch {
      /* a broken event must not kill the stream */
    }
  }
  return out;
}

module.exports = { GuildClient, ApiError, parseSseBlock };
