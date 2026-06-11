#!/usr/bin/env python3
"""A very light localhost viewer for a delegate work_dir.

Reads the same <work_dir>/.delegate/ state the MCP writes (no new deps, stdlib
only) and serves a single auto-refreshing page showing the agent roster, the
shared blackboard, the live event log, and total spend. A message box lets YOU
post onto the message bus (messages.jsonl) that agents read and reply to
via their post_message tool — your channel into the swarm.

    python viewer.py <work_dir> [port]      # default port 7878

Run as a separate process from the MCP server; they share state through the
files on disk.
"""

import os
import sys
import json
import http.server
import socketserver

import coordination as coord
import messages as msgbus
import ledger

WORK = os.path.abspath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "."))
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 7878

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>delegate viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
         background:#0d1117; color:#c9d1d9; }
  header { padding:10px 16px; border-bottom:1px solid #21262d; display:flex;
           align-items:center; gap:16px; }
  header b { color:#e6edf3; font-size:15px; }
  header .dir { color:#8b949e; font-size:12px; }
  header .spend { margin-left:auto; color:#3fb950; }
  .grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:1px; background:#21262d;
          height:calc(100vh - 47px - 110px); }
  .col { background:#0d1117; overflow:auto; padding:10px 12px; }
  .col h2 { margin:0 0 8px; font-size:11px; text-transform:uppercase; letter-spacing:.08em;
            color:#8b949e; position:sticky; top:0; background:#0d1117; padding-bottom:4px; }
  .agent { border:1px solid #21262d; border-radius:6px; padding:8px; margin-bottom:6px; }
  .agent .id { color:#e6edf3; font-weight:600; }
  .agent .task { color:#8b949e; font-size:12px; margin-top:2px; word-break:break-word; }
  .pill { display:inline-block; padding:0 6px; border-radius:10px; font-size:11px; }
  .running { background:#1f6feb33; color:#58a6ff; }
  .applied,.done { background:#23863633; color:#3fb950; }
  .failed { background:#da363333; color:#f85149; }
  .skipped,.incomplete,.pending { background:#9e6a0333; color:#d29922; }
  .ev { padding:2px 0; border-bottom:1px solid #161b22; white-space:pre-wrap; word-break:break-word; }
  .ev .t { color:#8b949e; }
  .ev .a { color:#58a6ff; }
  pre { white-space:pre-wrap; word-break:break-word; margin:0; }
  .feed { height:110px; border-top:1px solid #21262d; display:flex; flex-direction:column; }
  .msgs { flex:1; overflow:auto; padding:8px 12px; }
  .msg { margin-bottom:4px; }
  .msg .from { color:#d2a8ff; }
  .msg.human .from { color:#3fb950; }
  form { display:flex; gap:8px; padding:8px 12px; border-top:1px solid #21262d; }
  input { flex:1; background:#0d1117; border:1px solid #30363d; color:#c9d1d9;
          border-radius:6px; padding:6px 10px; font:inherit; }
  button { background:#238636; border:0; color:#fff; border-radius:6px; padding:6px 14px;
           font:inherit; cursor:pointer; }
</style></head><body>
<header><b>delegate</b><span class="dir" id="dir"></span><span class="spend" id="spend"></span></header>
<div class="grid">
  <div class="col"><h2>Agents</h2><div id="agents"></div></div>
  <div class="col"><h2>Blackboard</h2><pre id="board"></pre></div>
  <div class="col"><h2>Events</h2><div id="events"></div></div>
</div>
<div class="feed">
  <div class="msgs" id="msgs"></div>
  <form onsubmit="return send(event)">
    <input id="text" placeholder="message the agents…" autocomplete="off">
    <button>Send</button>
  </form>
</div>
<script>
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){
  let s; try { s = await (await fetch('/api/state')).json(); } catch(e){ return; }
  document.getElementById('dir').textContent = s.work_dir;
  document.getElementById('spend').textContent =
    '$'+(s.spend.usd||0).toFixed(4)+' · '+(s.spend.calls||0)+' calls';
  const reg = s.registry||{};
  document.getElementById('agents').innerHTML = Object.keys(reg).length
    ? Object.entries(reg).map(([id,a])=>`<div class="agent"><span class="id">${esc(id)}</span>
        <span class="pill ${esc(a.status||'')}">${esc(a.status||'?')}</span>
        ${a.attempts!=null?`<span class="pill">try ${a.attempts}</span>`:''}
        <div class="task">${esc(a.task||'')}</div>
        ${a.output_path?`<div class="task">→ ${esc(a.output_path)}</div>`:''}
        ${a.error?`<div class="task" style="color:#f85149">${esc(a.error)}</div>`:''}</div>`).join('')
    : '<div class="task">no agents yet</div>';
  const board = {...(s.board||{})};
  document.getElementById('board').textContent =
    Object.keys(board).length ? JSON.stringify(board,null,2) : '(empty)';
  document.getElementById('events').innerHTML = (s.events||[]).slice().reverse().map(e=>
    `<div class="ev"><span class="t">${esc((''+e.type).padEnd(11))}</span> `+
    `<span class="a">${esc(e.agent||'')}</span> ${esc(e.key||e.status||e.hook||e.reason||e.to||'')}</div>`).join('');
  const msgs = s.messages||[];
  const box = document.getElementById('msgs');
  const atBottom = box.scrollHeight-box.scrollTop-box.clientHeight < 40;
  box.innerHTML = msgs.map(m=>`<div class="msg ${m.from==='human'?'human':''}">`+
    `<span class="from">${esc(m.from)}</span>${m.to?`<span class="from">→${esc(m.to)}</span>`:''}: ${esc(m.text)}</div>`).join('');
  if(atBottom) box.scrollTop = box.scrollHeight;
}
async function send(e){
  e.preventDefault();
  const i = document.getElementById('text'); const t = i.value.trim();
  if(!t) return false;
  i.value='';
  await fetch('/api/msg',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({text:t})});
  tick(); return false;
}
tick(); setInterval(tick, 1500);
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            return self._send(
                200,
                json.dumps(
                    {
                        "work_dir": WORK,
                        "registry": coord.reg_get(WORK),
                        "board": coord.board_get(WORK),
                        "events": coord.events_tail(WORK, 200),
                        "messages": msgbus.read_messages(WORK),
                        "spend": ledger.spend_summary(WORK),
                    }
                ),
            )
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/msg":
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            text = (body.get("text") or "").strip()
            to = (body.get("to") or "").strip()
            if text:
                msgbus.post_message(WORK, "human", text, to)
            return self._send(200, json.dumps({"ok": bool(text)}))
        return self._send(404, "not found", "text/plain")

    def log_message(self, *a):  # keep the console quiet
        pass


if __name__ == "__main__":
    if not os.path.isdir(WORK):
        sys.exit(f"work_dir not found: {WORK}")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"delegate viewer → http://127.0.0.1:{PORT}  (work_dir: {WORK})")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
