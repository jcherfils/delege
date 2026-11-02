"""``delege settings``: one local web page (standard-library HTTP server on 127.0.0.1, a random port, a secret
token in the URL and in every request) — the providers with their key, "Create a key…" and "Policy" links, each
model's on / off and *Test* (a 1-token probe), today's usage, the local Ollama's status, the machines, the parallel
and cloud switches. Nothing listens beyond localhost; the keys go to the OS keyring or ``secrets.json`` (0600) and
are never sent back to the page (only where they were found)."""
from __future__ import annotations

import html
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import cloud
from .doctor import ollama_status
from .machine import HOST_KEYS, OLLAMA_URL, host_from_json, raw_config, save_raw_config

POLICY_URL = "https://github.com/jcherfils/delege#privacy"


class Settings:
    """The state behind the page, usable without the server (tests, other front ends)."""

    def __init__(self, probe=None, ollama=None):
        self.probe = probe or cloud.probe
        self.ollama = ollama or ollama_status

    # -------------------------------------------------------------- read
    def state(self, with_ollama: bool = True) -> dict:
        cfg = raw_config()
        s = cloud.load_settings()
        usage = cloud.Usage()
        providers = []
        for p in cloud.PROVIDERS.values():
            if p.local:
                continue
            models = []
            for m in cloud.MODELS.values():
                if m.provider != p.id:
                    continue
                req, tok = usage.counts(m.id)
                models.append({"id": m.id, "name": m.name, "limits": m.limits.text(), "strength": m.strength,
                               "vision": m.vision, "trains": m.trains, "policy": m.policy,
                               "enabled": cloud.enabled(m, s), "requests": req, "tokens": tok,
                               "exhausted": usage.exhausted(m) or ""})
            providers.append({"id": p.id, "name": p.name, "key": cloud.key_for(p.id)[1], "key_env": p.key_env,
                              "key_url": p.key_url, "policy": p.policy, "policy_url": p.policy_url,
                              "free_url": p.free_url, "note": p.note, "models": models})
        hosts = [host_from_json(h).to_json() for h in cfg.get("hosts") or [] if isinstance(h, dict) and h.get("name")]
        out = {"cloud": bool(s["cloud"]), "parallel": max(1, int(cfg.get("parallel") or 1)),
               "identity": cfg.get("identity") or {}, "model": cfg.get("model"),
               "timeout_min": int(cfg.get("timeout_min") or 60), "ollama_url": cfg.get("ollama_url") or OLLAMA_URL,
               "hosts": hosts, "providers": providers, "keyring": cloud._keyring() is not None,
               "parallelism": self.parallelism(s)}
        if with_ollama:
            out["ollama"] = self.ollama(out["ollama_url"])
        return out

    def parallelism(self, s: dict | None = None) -> int:
        """Tasks at once with cloud models on: the local model plus one per ready hosted model (a key, on, not
        exhausted)."""
        s = s if s is not None else cloud.load_settings()
        usage = cloud.Usage()
        n = 0
        for m in cloud.MODELS.values():
            if not m.local and cloud.enabled(m, s) and cloud.key_for(m.provider)[1] and not usage.exhausted(m):
                n += 1
        return 1 + n

    # -------------------------------------------------------------- write
    def set_key(self, provider_id: str, key: str) -> str:
        if provider_id not in cloud.PROVIDERS or cloud.PROVIDERS[provider_id].local:
            raise ValueError(f"unknown provider {provider_id}")
        return cloud.set_key(provider_id, key)

    def set_model(self, model_id: str, on: bool):
        if model_id not in cloud.MODELS:
            raise ValueError(f"unknown model {model_id}")
        s = cloud.load_settings()
        s["models"][model_id] = bool(on)
        cloud.save_settings(s)

    def set_cloud(self, on: bool):
        s = cloud.load_settings()
        s["cloud"] = bool(on)
        cloud.save_settings(s)

    def set_option(self, key: str, value):
        """``parallel`` (int ≥ 1), ``model`` (str or null), ``timeout_min`` (int), ``ollama_url``, ``identity``."""
        cfg = raw_config()
        if key == "parallel":
            cfg["parallel"] = max(1, int(value))
        elif key == "timeout_min":
            cfg["timeout_min"] = max(1, int(value))
        elif key == "model":
            cfg["model"] = str(value).strip() or None
        elif key == "ollama_url":
            cfg["ollama_url"] = str(value).strip() or OLLAMA_URL
        elif key == "identity":
            if not isinstance(value, dict) or not value.get("name") or not value.get("email"):
                raise ValueError("identity needs a name and an email")
            cfg["identity"] = {"name": str(value["name"]), "email": str(value["email"])}
        else:
            raise ValueError(f"unknown option {key}")
        save_raw_config(cfg)

    def set_hosts(self, hosts: list):
        """Replaces the machines list (each ``{name, ssh?, repos?, slots?, gpu_gb?, python?, opencode?, agents?}``)."""
        clean = []
        for h in hosts:
            if not isinstance(h, dict) or not str(h.get("name", "")).strip():
                raise ValueError("each machine needs a name")
            d = {k: v for k, v in h.items() if k in HOST_KEYS and v not in (None, "", [])}
            d["name"] = str(h["name"]).strip()
            if "slots" in d:
                d["slots"] = max(1, int(d["slots"]))
            if "gpu_gb" in d:
                d["gpu_gb"] = float(d["gpu_gb"])
            if "agents" in d and isinstance(d["agents"], str):
                d["agents"] = [a.strip() for a in d["agents"].split(",") if a.strip()]
            if "repos" in d and not isinstance(d["repos"], dict):
                raise ValueError("repos must be a table owner/name → path")
            clean.append(d)
        cfg = raw_config()
        cfg["hosts"] = clean
        save_raw_config(cfg)

    def test(self, model_id: str) -> dict:
        if model_id not in cloud.MODELS:
            raise ValueError(f"unknown model {model_id}")
        ok, msg = self.probe(model_id)
        return {"ok": bool(ok), "message": msg}


# ------------------------------------------------------------------ the page
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>delege settings</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--line:#d9d9d4;--ok:#1f7a3a;--warn:#a65d00;--bad:#b3261e;--card:#fff;--acc:#2a5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#e8e8e4;--mut:#9a9a94;--line:#34353a;--card:#1f2024;--acc:#7aa2ff;--ok:#5fcf7f;--warn:#ffb454;--bad:#ff7b72}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;max-width:960px;margin-inline:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p.mut,small,.mut{color:var(--mut)}
section.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.row{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:6px 0}
input[type=text],input[type=password],input[type=number]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--acc)}a{color:var(--acc)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:500}
code{font-size:13px}.model-name{font-weight:500}#msg{position:fixed;bottom:12px;right:12px;background:var(--card);border:1px solid var(--line);padding:8px 12px;border-radius:8px;display:none}
label.sw{display:inline-flex;gap:6px;align-items:center}
</style></head><body>
<h1>delege settings</h1>
<p class="mut">This page is served on this machine only (127.0.0.1). Keys are stored in the OS keyring when available,
else in <code>~/.config/delege/secrets.json</code> (mode 0600); they never enter a repository and are never shown here.</p>
<div id="app">loading…</div>
<div id="msg"></div>
<script>
const TOKEN = __TOKEN__;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S = null;
function say(t, cls){const m=$("#msg");m.textContent=t;m.className=cls||"";m.style.display="block";setTimeout(()=>m.style.display="none",4000);}
async function api(path, body){
  const r = await fetch("/api/"+path, {method: body===undefined?"GET":"POST", headers:{"X-Token":TOKEN,"Content-Type":"application/json"}, body: body===undefined?undefined:JSON.stringify(body)});
  const j = await r.json(); if(!r.ok){ say(j.error||r.statusText, "bad"); throw new Error(j.error); } return j;
}
async function load(){ S = await api("state"); render(); }
function render(){
  const s = S; let h = "";
  h += `<section class="card"><h2 style="margin-top:0">Delegated runs</h2>
    <div class="row"><label class="sw"><input type="checkbox" id="cloud" ${s.cloud?"checked":""}> Use cloud models for delegated tasks (<code>delege run --cloud</code> does it once)</label></div>
    <div class="row">Tasks at once with cloud models: <b>${s.parallelism}</b> <span class="mut">(the local model + ${s.parallelism-1} hosted model(s) ready now)</span></div>
    <div class="row"><label>Parallel (local mode, tasks at once over all machines) <input type="number" min="1" id="parallel" value="${s.parallel}" style="width:70px"></label>
    <label>Timeout (min) <input type="number" min="1" id="timeout" value="${s.timeout_min}" style="width:80px"></label>
    <label>Model <input type="text" id="model" placeholder="opencode.jsonc's" value="${esc(s.model||"")}" style="width:220px"></label>
    <button onclick="saveOptions()">Save</button></div>
    <div class="row"><label>Commits by <input type="text" id="idname" value="${esc(s.identity.name||"OpenCode")}" style="width:160px"></label>
    <input type="text" id="idemail" value="${esc(s.identity.email||"opencode@localhost")}" style="width:220px"> <button onclick="saveIdentity()">Save identity</button></div>
    <p class="mut">What leaves this machine with cloud models: the task's text (the issue) and the repository code the agent reads — never the forbidden folders, a model file or a video. <a href="${POLICY}" target="_blank">Privacy</a></p>
  </section>`;
  const o = s.ollama || {};
  h += `<section class="card"><h2 style="margin-top:0">Local model (Ollama)</h2>
    <div class="row">${o.up?`<span class="ok">● running</span> at <code>${esc(s.ollama_url)}</code> — models: ${o.models.map(m=>"<code>"+esc(m)+"</code>").join(", ")||"none"}; loaded: ${o.loaded.map(m=>"<code>"+esc(m)+"</code>").join(", ")||"none"}`
      :`<span class="bad">● not reachable</span> at <code>${esc(s.ollama_url)}</code> <span class="mut">${esc(o.error||"")}</span>`}</div>
    <div class="row"><label>Ollama URL <input type="text" id="ollama_url" value="${esc(s.ollama_url)}" style="width:260px"></label> <button onclick="saveOllama()">Save</button> <span class="mut">run <code>delege doctor</code> for the full check</span></div></section>`;
  h += `<section class="card"><h2 style="margin-top:0">Machines</h2><table id="hosts"><tr><th>name</th><th>ssh (user@host; empty = this machine)</th><th>slots</th><th>GPU GB</th><th>python</th><th>opencode</th><th>agents (comma)</th><th>repos (owner/name=path; …)</th><th></th></tr>`;
  s.hosts.forEach((x,i)=>{ h += hostRow(x,i); });
  h += `</table><div class="row"><button onclick="addHost()">Add a machine</button><button onclick="saveHosts()">Save machines</button>
    <span class="mut">An ssh machine needs a clone of each project (<code>repos</code>, default <code>~/&lt;project&gt;</code>), OpenCode 2 and Ollama there, and key-based ssh from here.</span></div></section>`;
  h += `<h2>Hosted models with a free tier</h2><p class="mut">Only tiers with a declared free limit are listed; a model is used only when its provider has a key, it is on, and today's count is under its limit. Models whose provider may train on the prompts are off until you switch them on.</p>`;
  for(const p of s.providers){
    h += `<section class="card"><div class="row"><b>${esc(p.name)}</b> ${p.key?`<span class="ok">✓ key (${esc(p.key)})</span> <button onclick="forgetKey('${p.id}')">Forget</button>`:`<span class="warn">no key</span>`}
      <a href="${esc(p.key_url)}" target="_blank">Create a key…</a> <a href="${esc(p.policy_url)}" target="_blank">Policy</a> ${p.free_url?`<a href="${esc(p.free_url)}" target="_blank">Free tier</a>`:""}</div>
      <div class="row"><input type="password" id="key-${p.id}" placeholder="paste the key (or export ${esc(p.key_env)}=…)" style="width:360px" autocomplete="off"> <button onclick="saveKey('${p.id}')">Save key</button></div>
      <p class="mut" style="margin:4px 0">Data: ${esc(p.policy)}${p.note?" — "+esc(p.note):""}</p>
      <table><tr><th>on</th><th>model</th><th>free limits</th><th>today</th><th>data</th><th></th></tr>`;
    for(const m of p.models){
      h += `<tr><td><input type="checkbox" ${m.enabled?"checked":""} onchange="setModel('${m.id}', this.checked)"></td>
        <td><span class="model-name">${esc(m.name)}</span><br><code>${esc(m.id)}</code> <small>strength ${m.strength}${m.vision?", vision":""}</small></td>
        <td>${esc(m.limits)}</td><td>${m.requests} req / ${m.tokens} tok${m.exhausted?`<br><span class="warn">${esc(m.exhausted)}</span>`:""}</td>
        <td><small>${esc(m.policy||"as the provider's")}${m.trains?' <span class="warn">(may train)</span>':""}</small></td>
        <td><button onclick="test('${m.id}', this)">Test</button> <span id="t-${CSS.escape(m.id)}"></span></td></tr>`;
    }
    h += `</table></section>`;
  }
  $("#app").innerHTML = h;
  $("#cloud").onchange = async (e) => { await api("cloud", {on: e.target.checked}); say("cloud switch saved"); load(); };
}
function hostRow(x,i){
  const repos = Object.entries(x.repos||{}).map(([k,v])=>k+"="+v).join("; ");
  return `<tr data-i="${i}"><td><input type="text" class="h-name" value="${esc(x.name)}" style="width:90px"></td><td><input type="text" class="h-ssh" value="${esc(x.ssh||"")}" style="width:150px"></td>
   <td><input type="number" class="h-slots" min="1" value="${x.slots||1}" style="width:55px"></td><td><input type="number" class="h-gpu" step="0.1" value="${x.gpu_gb??""}" style="width:65px"></td>
   <td><input type="text" class="h-python" value="${esc(x.python||"")}" style="width:120px"></td><td><input type="text" class="h-opencode" value="${esc(x.opencode||"")}" style="width:110px"></td>
   <td><input type="text" class="h-agents" value="${esc((x.agents||[]).join(", "))}" style="width:130px"></td><td><input type="text" class="h-repos" value="${esc(repos)}" style="width:200px"></td>
   <td><button onclick="this.closest('tr').remove()">×</button></td></tr>`;
}
function addHost(){ const t=$("#hosts"); t.insertAdjacentHTML("beforeend", hostRow({name:"", slots:1}, t.rows.length)); }
async function saveHosts(){
  const hosts = [...$("#hosts").querySelectorAll("tr[data-i]")].map(tr => {
    const v = c => tr.querySelector("."+c).value.trim();
    const repos = {}; v("h-repos").split(";").map(s=>s.trim()).filter(Boolean).forEach(s=>{const [k,...r]=s.split("="); if(k&&r.length) repos[k.trim()]=r.join("=").trim();});
    return {name:v("h-name"), ssh:v("h-ssh"), slots:+v("h-slots")||1, gpu_gb:v("h-gpu")===""?null:+v("h-gpu"), python:v("h-python"), opencode:v("h-opencode"), agents:v("h-agents"), repos};
  });
  await api("hosts", {hosts}); say("machines saved"); load();
}
async function saveOptions(){ await api("option",{key:"parallel",value:+$("#parallel").value}); await api("option",{key:"timeout_min",value:+$("#timeout").value}); await api("option",{key:"model",value:$("#model").value}); say("saved"); load(); }
async function saveIdentity(){ await api("option",{key:"identity",value:{name:$("#idname").value,email:$("#idemail").value}}); say("identity saved"); }
async function saveOllama(){ await api("option",{key:"ollama_url",value:$("#ollama_url").value}); say("saved"); load(); }
async function saveKey(p){ const k=$("#key-"+p).value; if(!k.trim()){say("paste a key first","warn");return;} const r=await api("key",{provider:p,key:k}); say("key saved in the "+r.where); load(); }
async function forgetKey(p){ await api("key",{provider:p,key:""}); say("key removed"); load(); }
async function setModel(id,on){ await api("model",{id,enabled:on}); say((on?"on: ":"off: ")+id); S.parallelism = (await api("state")).parallelism; }
async function test(id, btn){ const out=document.getElementById("t-"+id); out.textContent="…"; btn.disabled=true; try{ const r=await api("test",{id}); out.innerHTML=(r.ok?'<span class="ok">✓ ':'<span class="bad">✗ ')+esc(r.message)+"</span>"; } finally { btn.disabled=false; } }
load();
</script></body></html>
"""


class Server:
    """The HTTP server: GET ``/?token=…`` the page, GET ``/api/state``, POST ``/api/{key,model,cloud,option,hosts,
    test}`` with the token in ``X-Token``. Requests whose ``Host`` is not this loopback address are refused (DNS
    rebinding), as is any request without the token."""

    def __init__(self, settings: Settings | None = None, port: int = 0):
        self.settings = settings or Settings()
        self.token = secrets.token_urlsafe(24)
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _host_ok(self) -> bool:
                h = (self.headers.get("Host") or "").split(":")[0]
                return h in ("127.0.0.1", "localhost")

            def do_GET(self):
                if not self._host_ok():
                    return self._json(403, {"error": "forbidden"})
                url = urlsplit(self.path)
                if url.path == "/":
                    tok = parse_qs(url.query).get("token", [""])[0]
                    if not secrets.compare_digest(tok, srv.token):
                        return self._send(403, b"forbidden: open the URL printed by `delege settings`", "text/plain")
                    page = PAGE.replace("__TOKEN__", json.dumps(srv.token))
                    page = page.replace("${POLICY}", html.escape(POLICY_URL))
                    return self._send(200, page.encode(), "text/html; charset=utf-8")
                if url.path == "/api/state":
                    if not self._auth():
                        return
                    return self._json(200, srv.settings.state())
                self._json(404, {"error": "not found"})

            def _auth(self) -> bool:
                if secrets.compare_digest(self.headers.get("X-Token") or "", srv.token):
                    return True
                self._json(403, {"error": "bad token"})
                return False

            def do_POST(self):
                if not self._host_ok():
                    return self._json(403, {"error": "forbidden"})
                if not self._auth():
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    out = srv.handle(urlsplit(self.path).path, body)
                except (ValueError, TypeError, KeyError) as e:
                    return self._json(400, {"error": str(e)})
                self._json(200, out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.httpd.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def handle(self, path: str, body: dict) -> dict:
        s = self.settings
        if path == "/api/key":
            return {"where": s.set_key(str(body["provider"]), str(body.get("key", "")))}
        if path == "/api/model":
            s.set_model(str(body["id"]), bool(body.get("enabled")))
            return {"ok": True}
        if path == "/api/cloud":
            s.set_cloud(bool(body.get("on")))
            return {"ok": True}
        if path == "/api/option":
            s.set_option(str(body["key"]), body.get("value"))
            return {"ok": True}
        if path == "/api/hosts":
            s.set_hosts(list(body.get("hosts") or []))
            return {"ok": True}
        if path == "/api/test":
            return s.test(str(body["id"]))
        raise KeyError(f"no such action {path}")

    def serve_forever(self):
        self.httpd.serve_forever()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(open_browser: bool = True, out=print) -> int:
    srv = Server()
    out(f"delege settings: {srv.url}   (this machine only; Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(srv.url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.httpd.server_close()
    return 0
