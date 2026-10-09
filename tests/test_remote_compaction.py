"""Exercise the remote UI's actual compaction handlers without a browser/server."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for remote UI JavaScript")
def test_remote_compaction_stream_reset_snapshot_and_cleanup():
    source = (Path(__file__).resolve().parents[1] / "ddtui/remote_web/index.html").read_text()
    functions = []
    for name in ("handleMessage", "renderCompaction", "createFoldableBlock", "renderMessageText", "updateFoldCount", "charCount", "formatCount"):
        match = re.search(rf"^    function {name}\([^\n]*\) \{{.*?^    \}}", source, re.M | re.S)
        assert match, name
        functions.append(match.group())
    script = r'''
const assert = require("node:assert/strict");
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parent = null;
    this.className = ""; this.textContent = "";
    this.classList = { add() {}, remove() {} };
  }
  get isConnected() { return this.tag === "log" || !!(this.parent && this.parent.isConnected); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) { node.remove(); this.children.push(node); node.parent = this; }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  querySelector(selector) {
    for (const node of this.children) {
      if (`.${node.className}` === selector) return node;
      const child = node.querySelector(selector);
      if (child) return child;
    }
  }
  set innerHTML(value) { throw new Error("Compaction must render untrusted output as text"); }
}
const document = { createElement: tag => new Element(tag) };
const els = { log: new Element("log") };
let activeSession = "session-test", lastSeq = 0, messages = [], activeStatus = null;
let streaming = null, streamingNodes = null, compaction = null, compactionNode = null;
function scrollLogToEnd() {}
function renderStatus() { renderCompaction(); }
function renderMessages() { renderCompaction(); }
'''
    script += "\n".join(functions)
    script += r'''
function event(type, payload) {
  handleMessage({ kind: "event", session_id: activeSession, type, payload });
}
event("compaction.progress", { kind: "start", text: "分段摘要 · 1/2", automatic: true });
event("compaction.progress", { kind: "reasoning", text: "查证据", reasoning_chars: 3 });
event("compaction.progress", { kind: "content", text: "摘要🧪", content_chars: 3, reasoning_chars: 3 });
const first = compactionNode;
assert.equal(first.root.open, true);
assert.ok(first.text.textContent.includes("摘要🧪"));
assert.ok(first.count.textContent.includes("摘要 3 字符"));
assert.ok(first.root.querySelector(".role").textContent.includes("分段摘要 · 1/2"));
assert.equal(messages.length, 0); // compact previews are not assistant messages
first.root.open = false;
event("compaction.progress", { kind: "content", text: "<script>data</script>", content_chars: 24, reasoning_chars: 3 });
assert.equal(compactionNode, first);
assert.equal(first.root.open, false); // preserve the user's fold choice
assert.ok(first.text.textContent.includes("<script>data</script>"));
event("compaction.progress", { kind: "start", text: "合并分段摘要…" });
assert.equal(compaction.content, "");
assert.equal(compaction.reasoning, "");
event("session.snapshot", { status: { compacting: true }, messages: [], compaction: {
  phase: "生成工作状态摘要…", content: "恢复预览", reasoning: "", content_chars: 4
}});
assert.ok(compactionNode.text.textContent.includes("恢复预览"));
event("compaction.progress", { kind: "content", text: "继续", content_chars: 6 });
assert.equal(compaction.content, "恢复预览继续");
event("compaction.finished", { outcome: "failed" });
assert.equal(compaction, null);
assert.equal(compactionNode, null);
assert.equal(els.log.children.length, 0);
assert.equal(messages.length, 0);
console.log("Remote compaction streaming: OK");
'''
    result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
