'use strict';
// A scripted OpenAI-compatible model server (codeguild's "custom" provider points at it), so the
// whole chain — extension → codeguild server → model → tools → git — runs for real in tests.

const http = require('http');

const ROLES = [
  ['Project Lead', 'lead'],
  ['Software Engineer', 'engineer'],
  ['Critic', 'critic'],
  ['Verifier', 'verifier'],
  ['Security', 'security'],
  ['Technical Writer', 'docs'],
  ['Pair Programmer', 'pair'],
];

const PLAN = {
  roadmap: ['calculator basics'],
  tasks: [
    {
      id: 'T1',
      title: 'add subtract',
      description: 'Create calc.py with subtract(a, b) and a test_calc.py that tests it.',
      files: ['calc.py', 'test_calc.py'],
      done_when: 'test_subtract passes',
      depends_on: [],
    },
  ],
  risks: [],
  questions_for_owner: [],
};

function reply(body) {
  const sys = String((body.messages[0] || {}).content || '').slice(0, 300);
  const role = (ROLES.find(([title]) => sys.includes(title)) || [null, 'assistant'])[1];
  const lastUser = [...body.messages].reverse().find((m) => m.role === 'user') || {};
  const usedTools = body.messages.some((m) => m.role === 'tool');
  switch (role) {
    case 'lead':
      if (/REVIEW this task/.test(lastUser.content)) {
        return { text: JSON.stringify({ decision: 'ACCEPT', notes: 'good', improvements: [] }) };
      }
      return { text: JSON.stringify(PLAN) };
    case 'engineer':
      if (!usedTools) {
        return {
          tools: [
            ['write_file', { path: 'calc.py', content: 'def subtract(a, b):\n    return a - b\n' }],
            [
              'write_file',
              {
                path: 'test_calc.py',
                content: 'from calc import subtract\n\n\ndef test_subtract():\n    assert subtract(5, 3) == 2\n',
              },
            ],
          ],
        };
      }
      return {
        text: JSON.stringify({
          status: 'done',
          summary: 'added subtract',
          files_changed: ['calc.py', 'test_calc.py'],
          tests_run: 'passed',
        }),
      };
    case 'critic':
    case 'security':
      return { text: JSON.stringify({ verdict: 'approve', findings: [], summary: 'fine' }) };
    case 'pair': {
      if (!usedTools) {
        if (/slow/.test(lastUser.content)) return { tools: [['run_command', { command: 'python -c "import time; time.sleep(60)"' }]] };
        return { tools: [['write_file', { path: 'hello.py', content: "print('hello')\n" }]] };
      }
      const lastTool = [...body.messages].reverse().find((m) => m.role === 'tool') || {};
      return { text: /^refused/.test(lastTool.content || '') ? 'Okay, I left it alone.' : 'Created hello.py.' };
    }
    case 'docs':
      return { text: JSON.stringify({ files_changed: [], summary: 'nothing to document' }) };
    default:
      return { text: 'This project has app.py with an add function.' };
  }
}

function start() {
  const requests = [];
  const srv = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) {
        res.writeHead(404).end('{}');
        return;
      }
      const body = JSON.parse(raw);
      requests.push(body);
      const r = reply(body);
      const base = { id: 'x', created: 0, model: body.model };
      // like Ollama: every tool call of the turn is streamed with index 0
      const toolCalls = (r.tools || []).map(([name, args], i) => ({
        index: 0,
        id: `call_${requests.length}_${i}`,
        type: 'function',
        function: { name, arguments: JSON.stringify(args) },
      }));
      if (!body.stream) {
        const message = { role: 'assistant', content: r.text || null };
        if (toolCalls.length) message.tool_calls = toolCalls;
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            ...base,
            object: 'chat.completion',
            choices: [{ index: 0, message, finish_reason: toolCalls.length ? 'tool_calls' : 'stop' }],
            usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
          })
        );
        return;
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const send = (o) => res.write(`data: ${JSON.stringify(o)}\n\n`);
      const chunk = (delta, finish = null) =>
        send({ ...base, object: 'chat.completion.chunk', choices: [{ index: 0, delta, finish_reason: finish }] });
      if (r.text) {
        const mid = Math.ceil(r.text.length / 2);
        chunk({ role: 'assistant', content: r.text.slice(0, mid) });
        chunk({ content: r.text.slice(mid) });
      }
      for (const tc of toolCalls) chunk({ tool_calls: [tc] });
      chunk({}, toolCalls.length ? 'tool_calls' : 'stop');
      send({ ...base, object: 'chat.completion.chunk', choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      res.end('data: [DONE]\n\n');
    });
  });
  return new Promise((resolve) => {
    srv.listen(0, '127.0.0.1', () => resolve({ srv, requests, url: `http://127.0.0.1:${srv.address().port}/v1` }));
  });
}

module.exports = { start, PLAN };
