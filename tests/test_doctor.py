"""``delege doctor`` against a fake Ollama (a real HTTP server on 127.0.0.1), the fake opencode and gh."""
from __future__ import annotations

import json
import os
import threading

import pytest

from delege.doctor import Doctor, ollama_status

from .conftest import machine_config


class FakeOllama:
    """Ollama's /api/tags, /api/ps, /api/show, /api/generate on 127.0.0.1."""

    def __init__(self, ctx=65536, vram_share=1.0, native=262144, have=True):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.loaded, self.ctx, self.share, self.native, self.have, self.calls = False, ctx, vram_share, native, have, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                size = 19_000_000_000
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": "qwen3:14b"}] if fake.have else []})
                models = [{"name": "qwen3:14b", "model": "qwen3:14b", "size": size,
                           "size_vram": int(size * fake.share), "context_length": fake.ctx}] if fake.loaded else []
                self._send(200, {"models": models})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(("POST", self.path))
                if self.path == "/api/show":
                    if not fake.have:
                        return self._send(404, {"error": f"model '{body['model']}' not found"})
                    return self._send(200, {"model_info": {"qwen3moe.context_length": fake.native},
                                            "parameters": "stop \"<|im_end|>\""})
                fake.loaded = True
                return self._send(200, {"done": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def doctor_env(repo, fakes, monkeypatch):
    monkeypatch.setenv("PATH", f"{fakes['bin']}{os.pathsep}{os.environ['PATH']}")
    ollama = FakeOllama()
    machine_config(fakes, repo, hosts=[{"name": "rtx", "slots": 1, "gpu_gb": 32, "python": os.sys.executable,
                                        "opencode": fakes["opencode"]}], ollama_url=ollama.url)
    yield {"repo": repo, "ollama": ollama}
    ollama.close()


def _doctor(env, **kw):
    lines = []
    rc = Doctor(env["repo"], out=lines.append, **kw).run()
    return rc, "\n".join(lines)


def test_doctor_all_good(doctor_env):
    rc, out = _doctor(doctor_env)
    assert rc == 0, out
    assert "ok    project" in out and "agents tests, fix, lint, docs, checks, screenshots" in out
    assert "ok    config" in out and "ok    opencode   opencode v2.1.4" in out and "ok    python" in out
    assert "OpenCode sees tests, fix, lint, docs, checks, screenshots" in out
    assert "qwen3:14b in Ollama (262144 tokens" in out
    assert "runs with 65536 tokens of context" in out and "100% GPU" in out
    assert ("POST", "/api/generate") in doctor_env["ollama"].calls
    assert "#72 docs, #71 lint, #70 tests" in out
    assert "FAIL" not in out and "Ready:" in out


def test_doctor_says_how_to_fix(doctor_env, monkeypatch):
    o = doctor_env["ollama"]
    o.ctx, o.share = 32768, 0.8
    monkeypatch.setenv("FAKE_VERSION", "1.19.4")
    monkeypatch.setenv("FAKE_GH_AUTH", "0")
    rc, out = _doctor(doctor_env)
    assert rc == 1
    assert "OpenCode 1.19.4" in out and "fix: curl -fsSL https://opencode.ai/v2/install | bash" in out
    assert "32768 tokens of context (< 65536)" in out and 'Environment="OLLAMA_CONTEXT_LENGTH=65536"' in out
    assert "80% on the GPU" in out and "OLLAMA_KV_CACHE_TYPE=q8_0" in out
    assert "gh is not logged in" in out and "fix: gh auth login" in out
    assert "4 problem(s)" in out
    monkeypatch.delenv("FAKE_VERSION")
    monkeypatch.setenv("FAKE_GH_AUTH", "1")
    o.have = False
    rc, out = _doctor(doctor_env)
    assert rc == 1 and "fix: ollama pull qwen3:14b" in out
    o.close()
    rc, out = _doctor(doctor_env, load_model=False)
    assert "not reachable" in out and "fix: sudo systemctl enable --now ollama" in out
    assert not ollama_status(o.url)["up"]


def test_doctor_project_repo_and_agents(doctor_env, tmp_path):
    rc, out = _doctor({**doctor_env, "repo": doctor_env["repo"] / "tests"}, load_model=False)
    assert "FAIL  project" in out and "delege init" in out and "is not the root of a git clone" in out
    (doctor_env["repo"] / ".opencode" / "agents" / "lint.md").unlink()
    rc, out = _doctor(doctor_env, load_model=False)
    assert rc == 1 and "agents to refresh: lint: no .opencode/agents/lint.md" in out
    assert "OpenCode does not list lint" in out and "opencode reload" in out
    from delege import paths
    paths.config_file().unlink()
    rc, out = _doctor(doctor_env, load_model=False)
    assert "warn  config" in out and "delege settings" in out
    paths.config_file().write_text(json.dumps({"hosts": [{"name": "rtx", "python": str(tmp_path / "nopython"),
                                                           "agents": ["nope"]}]}))
    rc, out = _doctor(doctor_env, load_model=False)
    assert "FAIL  python" in out and "source .venv/bin/activate" in out and "unknown agent(s) nope" in out
