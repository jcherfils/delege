"""delege.cloud: the registry of free hosted models (only declared free limits), the key store (env, keyring,
secrets.json 0600, OpenCode's store), the daily usage and benching, the chat client against a fake
OpenAI-compatible server, the model pool overrides and ``delege models``."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from delege import cli, cloud, paths
from delege.machine import load_config, model_pool, usable_models

from .conftest import machine_config


def test_registry_only_free_tiers_with_limits_and_policies():
    for m in cloud.MODELS.values():
        p = cloud.PROVIDERS[m.provider]
        assert m.free, m.id
        assert m.limits.source and (m.policy or p.policy) and p.policy_url, m.id
        assert m.local == p.local
        if not p.local:
            assert p.key_env and p.key_url and p.base_url.startswith("https://"), m.id
    assert not cloud.model("acme/paid-model").free
    assert cloud.model("acme/paid-model").provider == "acme" and not cloud.model("acme/paid-model").local
    assert cloud.PROVIDERS["ollama"].local and cloud.MODELS["ollama/qwen3-coder:30b"].free
    assert not cloud.enabled(cloud.MODELS["google/gemini-2.5-flash"], {"models": {}})
    assert cloud.enabled(cloud.MODELS["groq/openai/gpt-oss-120b"], {"models": {}})
    assert cloud.enabled(cloud.MODELS["google/gemini-2.5-flash"], {"models": {"google/gemini-2.5-flash": True}})
    assert "Cerebras" in cloud.NOT_LISTED and "GitHub Models" in cloud.NOT_LISTED
    assert "20 req/min, 50 req/day" == cloud.PROVIDERS["openrouter"].limits.text()


def test_key_store_env_secrets_opencode(monkeypatch):
    assert cloud.key_for("groq") == (None, "")
    assert cloud.key_for("ollama") == ("", "local")
    assert cloud.set_key("groq", " gsk_abc ") == "secrets"
    assert cloud.key_for("groq") == ("gsk_abc", "secrets")
    mode = stat.S_IMODE(os.stat(paths.secrets_file()).st_mode)
    assert mode == 0o600, oct(mode)
    assert json.loads(paths.secrets_file().read_text()) == {"groq": "gsk_abc"}
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    assert cloud.key_for("groq") == ("env-key", "env")
    assert cloud.key_env()["GROQ_API_KEY"] == "env-key"
    monkeypatch.delenv("GROQ_API_KEY")
    auth = paths.opencode_auth()
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "or-key"}}))
    assert cloud.key_for("openrouter") == ("or-key", "opencode")
    assert cloud.key_env() == {"GROQ_API_KEY": "gsk_abc", "OPENROUTER_API_KEY": "or-key"}
    cloud.set_key("groq", "")
    assert cloud.key_for("groq") == (None, "") and "groq" not in json.loads(paths.secrets_file().read_text())


def test_key_store_keyring(monkeypatch):
    store = {}

    class Err(Exception):
        pass

    class KR:
        class errors:
            PasswordDeleteError = Err

        @staticmethod
        def get_password(svc, user):
            return store.get((svc, user))

        @staticmethod
        def set_password(svc, user, pw):
            store[(svc, user)] = pw

        @staticmethod
        def delete_password(svc, user):
            if (svc, user) not in store:
                raise Err()
            del store[(svc, user)]
    monkeypatch.setattr(cloud, "_keyring", lambda: KR)
    assert cloud.set_key("mistral", "m-key") == "keyring"
    assert store == {(cloud.KEYRING_SERVICE, "mistral"): "m-key"}
    assert cloud.key_for("mistral") == ("m-key", "keyring")
    assert not paths.secrets_file().exists()
    cloud.set_key("mistral", "")
    assert cloud.key_for("mistral") == (None, "")


@pytest.mark.skipif(sys.platform == "win32", reason="file modes")
def test_secrets_mode_fixed_on_rewrite():
    cloud.set_key("groq", "a")
    os.chmod(paths.secrets_file(), 0o644)
    cloud.set_key("mistral", "b")
    assert stat.S_IMODE(os.stat(paths.secrets_file()).st_mode) == 0o600


def test_settings_file_and_usage():
    s = cloud.load_settings()
    assert s == {"cloud": False, "models": {}}
    s["cloud"], s["models"]["groq/openai/gpt-oss-20b"] = True, False
    cloud.save_settings(s)
    assert cloud.load_settings()["cloud"] and not cloud.enabled(cloud.MODELS["groq/openai/gpt-oss-20b"])
    assert json.loads(paths.config_file().read_text())["enabled"] == {"groq/openai/gpt-oss-20b": False}
    u = cloud.Usage()
    m = cloud.MODELS["groq/openai/gpt-oss-20b"]
    assert u.exhausted(m) is None
    u.record(m.id, 999, 100)
    assert u.exhausted(m) is None
    u.record(m.id, 1, 0)
    assert "1000 requests today" in u.exhausted(m)
    a, b = cloud.MODELS["openrouter/poolside/laguna-s-2.1:free"], cloud.MODELS["openrouter/cohere/north-mini-code:free"]
    u.record(a.id, 30), u.record(b.id, 20)
    assert "shared free limit 50/day" in u.exhausted(b)
    g = cloud.MODELS["groq/openai/gpt-oss-120b"]
    u.bench(g.id, time.time() + 60, "HTTP 429")
    assert "benched until" in u.exhausted(g) and "HTTP 429" in u.exhausted(g)
    u2 = cloud.Usage()
    assert u2.counts(m.id) == (1000, 100) and u2.benched_until(g.id)
    u.bench(g.id, time.time() - 1)
    assert u.exhausted(g) is None
    assert not cloud.model("x/y").free and u.exhausted(cloud.model("x/y")) == "no declared free limit"
    assert cloud.next_reset(0) == 86400.0
    assert cloud.looks_rate_limited("Rate limit exceeded") and cloud.looks_rate_limited("", 429)
    assert cloud.looks_rate_limited("RESOURCE_EXHAUSTED: quota") and not cloud.looks_rate_limited("ok", 200)


class FakeAPI:
    """An OpenAI-compatible /chat/completions: answers "OK", or 429 for the models in ``limited``."""

    def __init__(self):
        self.calls, self.limited = [], set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if body["model"] in fake.limited:
                    raw = json.dumps({"error": {"message": "Rate limit exceeded", "code": 429}}).encode()
                    self.send_response(429)
                    self.send_header("Retry-After", "30")
                else:
                    raw = json.dumps({"model": body["model"], "choices": [{"message": {"content": "OK"}}],
                                      "usage": {"prompt_tokens": 7, "completion_tokens": 1}}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    f = FakeAPI()
    yield f
    f.close()


def test_chat_client_counts_usage_and_benches_on_429(api):
    u = cloud.Usage()
    mid = "groq/openai/gpt-oss-20b"
    with pytest.raises(cloud.CloudError, match="no API key"):
        cloud.chat(mid, "hi", base_url=api.url, usage=u)
    cloud.set_key("groq", "k1")
    a = cloud.chat(mid, "Say OK.", base_url=api.url, usage=u, max_tokens=1)
    assert a.text == "OK" and a.tokens == {"input": 7, "output": 1} and a.model == "openai/gpt-oss-20b"
    call = api.calls[-1]
    assert call["path"] == "/v1/chat/completions" and call["auth"] == "Bearer k1"
    assert call["body"]["model"] == "openai/gpt-oss-20b" and call["body"]["max_tokens"] == 1
    assert u.counts(mid) == (1, 8)
    ok, msg = cloud.probe(mid, base_url=api.url, usage=u)
    assert ok and "answered in" in msg
    api.limited.add("openai/gpt-oss-20b")
    with pytest.raises(cloud.CloudError) as e:
        cloud.chat(mid, "hi", base_url=api.url, usage=u)
    assert e.value.status == 429 and e.value.rate_limited and e.value.retry_after == 30.0
    until = u.benched_until(mid)
    assert until and 20 < until - time.time() <= 31
    assert u.counts(mid) == (2, 16)
    ok, msg = cloud.probe(mid, base_url=api.url, usage=u)
    assert not ok and "benched until" in msg
    with pytest.raises(cloud.CloudError, match="no declared free limit"):
        cloud.chat("groq/some-paid-model", "hi", base_url=api.url, usage=u)
    assert len(api.calls) == 3


def test_chat_images_for_vision_models(api, tmp_path):
    u = cloud.Usage()
    cloud.set_key("google", "g")
    png = tmp_path / "f.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8)
    a = cloud.chat("google/gemini-2.5-flash", "What is this?", [png, b"\xff\xd8\xff" + b"\0" * 4,
                   "https://example.org/x.jpg"], base_url=api.url, usage=u, system="Be short.")
    assert a.text == "OK"
    msgs = api.calls[-1]["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "Be short."}
    parts = msgs[1]["content"]
    assert parts[0] == {"type": "text", "text": "What is this?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[3]["image_url"]["url"] == "https://example.org/x.jpg"
    with pytest.raises(cloud.CloudError, match="takes no image"):
        cloud.chat("groq/openai/gpt-oss-20b", "x", [png], base_url=api.url, usage=u, key="k")


# ------------------------------------------------------------------ the pool and `delege models`
def test_model_pool_only_declared_free_limits():
    ids = [m.id for m in model_pool()]
    assert "groq/openai/gpt-oss-120b" in ids and "opencode/glm-5.3-flash-free" in ids
    assert all(m.free and not m.local for m in model_pool())
    pool = {m.id: m for m in model_pool([
        {"id": "groq/openai/gpt-oss-120b", "use": False},
        {"id": "groq/openai/gpt-oss-20b", "limits": {"rpd": 10}, "strength": 3},
        {"id": "acme/paid-model"},
        {"id": "openrouter/acme/new-model:free", "limits": {"rpd": 40, "rpm": 20}, "vision": True},
        "junk"])}
    assert "groq/openai/gpt-oss-120b" not in pool and "acme/paid-model" not in pool
    assert pool["groq/openai/gpt-oss-20b"].limits.rpd == 10 and pool["groq/openai/gpt-oss-20b"].strength == 3
    assert pool["groq/openai/gpt-oss-20b"].limits.tpd == 200_000
    assert pool["openrouter/acme/new-model:free"].vision and pool["openrouter/acme/new-model:free"].limits.rpd == 40
    assert load_config()["cloud"] is False


def test_models_command_lists_usable_models(repo, fakes, monkeypatch, capsys):
    cfg = machine_config(fakes, repo)
    monkeypatch.setenv("FAKE_MODELS", "ollama/qwen3-coder:30b,groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b,"
                                      "google/gemini-2.5-flash")
    monkeypatch.setenv("GROQ_API_KEY", "gsk")
    cloud.set_key("google", "g")
    probed = []
    monkeypatch.setattr(cloud, "probe", lambda mid, **kw: probed.append(mid) or (mid.endswith("-20b"), "fake"))
    rows = {m.id: st for m, st in usable_models(cfg, fakes["opencode"], probe=True)}
    assert rows["groq/openai/gpt-oss-20b"]["usable"] and rows["groq/openai/gpt-oss-20b"]["key"] == "env"
    assert not rows["groq/openai/gpt-oss-120b"]["usable"]
    assert rows["groq/qwen/qwen3.8-27b"]["listed"] is False
    assert not rows["google/gemini-2.5-flash"]["on"]
    assert rows["opencode/glm-5.3-flash-free"]["key"] == "" and not rows["opencode/glm-5.3-flash-free"]["usable"]
    assert set(probed) == {"groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"}
    cloud.Usage().record("groq/openai/gpt-oss-20b", 1000)
    rows = {m.id: st for m, st in usable_models(cfg, fakes["opencode"], probe=False)}
    assert "1000 requests today" in rows["groq/openai/gpt-oss-20b"]["exhausted"]
    monkeypatch.chdir(repo)
    assert cli.main(["models", "--no-probe"]) == 0
    out = capsys.readouterr().out
    assert "ok  groq/openai/gpt-oss-120b" in out and "1000 requests today" in out
    assert "no key: export OPENCODE_API_KEY" in out and "https://opencode.ai/auth" in out
    assert "off (may train" in out and "cloud parallelism 2" in out and "cloud mode is off" in out
