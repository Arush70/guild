'use strict';
// A small stand-in for the `vscode` module so the extension can be exercised with plain node.
// It records what the extension shows and lets a test script the user's answers.

const path = require('path');

class EventEmitter {
  constructor() {
    this.listeners = [];
    this.event = (fn) => {
      this.listeners.push(fn);
      return { dispose: () => {} };
    };
  }
  fire(v) {
    for (const l of this.listeners) l(v);
  }
}

class TreeItem {
  constructor(label, collapsibleState) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}
class ThemeIcon {
  constructor(id) {
    this.id = id;
  }
}
class RelativePattern {
  constructor(base, pattern) {
    this.base = base;
    this.pattern = pattern;
  }
}

const Uri = {
  file: (p) => ({ scheme: 'file', fsPath: p, path: p, toString: () => 'file://' + p }),
  parse: (s) => ({ scheme: s.split(':')[0], path: s, toString: () => s }),
  from: (o) => ({ ...o, toString: () => `${o.scheme}:${o.path}` }),
  joinPath: (base, ...segs) => Uri.file(path.join(base.fsPath, ...segs)),
};

const mock = {
  config: { command: '', autoStart: true, port: 0, profile: '', notifications: true },
  context: {}, // setContext keys
  commands: new Map(),
  executed: [], // [id, ...args] of commands that are not registered by the extension
  messages: [], // {level, message, actions}
  shownDocs: [],
  terminals: [],
  channel: null,
  tree: null,
  bar: null,
  webviewProvider: null,
  docProvider: null,
  extensions: {},
  // answer functions; a test replaces them
  onInput: () => undefined,
  onQuickPick: (items) => items[0],
  onMessage: () => undefined,
  workspaceFolder: null,
};

function show(level) {
  return (message, ...rest) => {
    const actions = rest.filter((r) => typeof r === 'string');
    mock.messages.push({ level, message, actions });
    return Promise.resolve(mock.onMessage(message, actions, level));
  };
}

const vscode = {
  __mock: mock,
  EventEmitter,
  TreeItem,
  ThemeIcon,
  RelativePattern,
  Uri,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  window: {
    createOutputChannel: () =>
      (mock.channel = { lines: [], appendLine(l) { this.lines.push(l); }, show() {}, dispose() {} }),
    createTreeView: (id, opts) => (mock.tree = { id, provider: opts.treeDataProvider, message: undefined, dispose() {} }),
    createStatusBarItem: () => (mock.bar = { text: '', tooltip: '', show() {}, dispose() {} }),
    registerWebviewViewProvider: (id, provider) => {
      mock.webviewProvider = provider;
      return { dispose() {} };
    },
    showInputBox: (opts) => Promise.resolve(mock.onInput(opts)),
    showQuickPick: (items, opts) => Promise.resolve(mock.onQuickPick(items, opts)),
    showInformationMessage: show('info'),
    showWarningMessage: show('warn'),
    showErrorMessage: show('error'),
    showTextDocument: (doc) => {
      mock.shownDocs.push(doc);
      return Promise.resolve({});
    },
    createTerminal: (o) => {
      const t = { ...o, sent: [], show() {}, sendText(s) { this.sent.push(s); } };
      mock.terminals.push(t);
      return t;
    },
  },
  workspace: {
    get workspaceFolders() {
      return mock.workspaceFolder ? [{ uri: Uri.file(mock.workspaceFolder) }] : undefined;
    },
    getConfiguration: () => ({ get: (k) => mock.config[k] }),
    registerTextDocumentContentProvider: (scheme, provider) => {
      mock.docProvider = provider;
      return { dispose() {} };
    },
    openTextDocument: (uri) =>
      Promise.resolve({ uri, getText: () => mock.docProvider.provideTextDocumentContent(uri) }),
    createFileSystemWatcher: () => ({ onDidCreate() {}, dispose() {} }),
  },
  commands: {
    registerCommand: (id, fn) => {
      mock.commands.set(id, fn);
      return { dispose() {} };
    },
    executeCommand: (id, ...args) => {
      if (id === 'setContext') {
        mock.context[args[0]] = args[1];
        return Promise.resolve();
      }
      const fn = mock.commands.get(id);
      if (fn) return Promise.resolve(fn(...args));
      mock.executed.push([id, ...args]);
      return Promise.resolve();
    },
  },
  extensions: { getExtension: (id) => mock.extensions[id] },
  env: { openExternal: (u) => Promise.resolve(mock.executed.push(['openExternal', u.toString()])) },
};

module.exports = vscode;
