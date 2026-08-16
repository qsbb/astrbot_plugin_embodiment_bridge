import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.scrollTop = 0;
    this.clientHeight = 100;
    this.className = "";
    this.textContent = "";
  }

  get scrollHeight() {
    return Math.max(100, this.children.length * 100);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  append(...children) {
    this.children.push(...children);
  }

  addEventListener() {}
  classList = { add() {}, remove() {}, toggle() {} };
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  },
  createElement() {
    return new Element();
  },
  querySelector() {
    return new Element();
  },
  addEventListener() {},
};
const storage = new Map();
const window = {
  localStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
  },
  setTimeout,
  clearTimeout,
  addEventListener() {},
};
const sourcePath = new URL("../pages/operator/app.js", import.meta.url);
let source = fs.readFileSync(sourcePath, "utf8");
source = source.replace(/\ninit\(\);\s*$/, "\n");
source += `\n globalThis.__bridgeTest = { renderDiagnosticEvents, setDiagnosticAutoScroll };\n`;
const context = { window, document, console, setTimeout, clearTimeout };
vm.runInNewContext(source, context, { filename: sourcePath.pathname });

const container = document.getElementById("diagnostics-events");
const api = context.__bridgeTest;
assert.ok(api);
api.setDiagnosticAutoScroll(false);
api.renderDiagnosticEvents([
  { event: "fast_action.started", component: "action", status: "processing" },
  { event: "fast_action.completed", component: "action", status: "selected" },
  { event: "reply.completed", component: "reply", status: "completed" },
]);
container.scrollTop = 37;
api.renderDiagnosticEvents([
  { event: "fast_action.started", component: "action", status: "processing" },
  { event: "fast_action.completed", component: "action", status: "selected" },
  { event: "reply.completed", component: "reply", status: "completed" },
  { event: "audio.upload.completed", component: "transport", status: "completed" },
]);
assert.equal(container.scrollTop, 37);
api.setDiagnosticAutoScroll(true);
assert.equal(container.scrollTop, container.scrollHeight);
api.setDiagnosticAutoScroll(false);
container.scrollTop = 0;
api.renderDiagnosticEvents([]);
assert.equal(container.scrollTop, 0);
