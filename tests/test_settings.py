"""The settings page: its state and writes without the server, then over HTTP on 127.0.0.1 with the token
(refused without it, or from another Host); the one-time migration of FiFoo's files; check-images."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from delege import cloud, paths
from delege.settings import Server, Settings


def test_settings_state_and_writes(monkeypatch):
    probed = []
    s = Settings(probe=lambda mid: probed.append(mid) or (mid.startswith("groq/"), "fake"),
                 ollama=lambda url: {"up": False, "models": [], "loaded": [], "error": "down"})
    st = s.state()
    assert [p["id"] for p in st["providers"]] == ["opencode", "openrouter", "google", "groq", "mistral"]
    assert all(p["key"] == "" for p in st["providers"]) and st["parallelism"] == 1 and st["cloud"] is False
    assert st["hosts"] == [] and st["parallel"] == 1 and not st["ollama"]["up"]
    groq = next(p for p in st["providers"] if p["id"] == "groq")
    assert groq["key_url"] == "https://console.groq.com/keys" and groq["policy_url"] and groq["models"]
    assert all(m["enabled"] for m in groq["models"])
    google = next(p for p in st["providers"] if p["id"] == "google")
    assert not any(m["enabled"] for m in google["models"]) and all(m["trains"] for m in google["models"])
    # a key: stored in secrets.json (no keyring here), never in the state
    assert s.set_key("groq", "gsk_1") == "secrets"
    assert json.loads(paths.secrets_file().read_text()) == {"groq": "gsk_1"}
    st = s.state(with_ollama=False)
    assert next(p for p in st["providers"] if p["id"] == "groq")["key"] == "secrets"
    assert "gsk_1" not in json.dumps(st)
    assert st["parallelism"] == 1 + len(groq["models"])
    m = groq["models"][0]["id"]
    s.set_model(m, False)
    assert not cloud.enabled(cloud.MODELS[m]) and s.parallelism() == len(groq["models"])
    s.set_cloud(True)
    assert cloud.load_settings()["cloud"] is True and s.state(with_ollama=False)["cloud"]
    assert s.test(m) == {"ok": True, "message": "fake"} and probed == [m]
    assert s.test(google["models"][0]["id"])["ok"] is False
    s.set_option("parallel", 3)
    s.set_option("identity", {"name": "Me", "email": "me@x.org"})
    s.set_option("model", "ollama/other")
    s.set_hosts([{"name": "rtx", "slots": 2, "gpu_gb": "32", "agents": "tests, docs", "ssh": ""},
                 {"name": "box", "ssh": "jm@box", "repos": {"o/r": "~/r"}}])
    cfg = json.loads(paths.config_file().read_text())
    assert cfg["parallel"] == 3 and cfg["identity"] == {"name": "Me", "email": "me@x.org"} and cfg["model"] == "ollama/other"
    assert cfg["hosts"] == [{"name": "rtx", "slots": 2, "gpu_gb": 32.0, "agents": ["tests", "docs"]},
                            {"name": "box", "ssh": "jm@box", "repos": {"o/r": "~/r"}}]
    assert cfg["cloud"] is True and cfg["enabled"] == {m: False}             # the other keys kept
    assert [h["name"] for h in s.state(with_ollama=False)["hosts"]] == ["rtx", "box"]
    s.set_key("groq", "")
    assert s.state(with_ollama=False)["parallelism"] == 1
    for bad in (lambda: s.set_key("nope", "k"), lambda: s.set_model("x/y", True), lambda: s.set_option("zz", 1),
                lambda: s.set_hosts([{"slots": 1}]), lambda: s.set_option("identity", {"name": "x"})):
        with pytest.raises(ValueError):
            bad()


def _req(url, token=None, body=None, host=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"X-Token": token} if token else {}),
                                          **({"Host": host} if host else {})},
                                 method="GET" if body is None else "POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_server_serves_localhost_only_with_a_token():
    srv = Server(Settings(probe=lambda mid: (True, "fake"),
                          ollama=lambda url: {"up": True, "models": ["q"], "loaded": [], "error": ""}))
    srv.start()
    try:
        assert srv.url.startswith("http://127.0.0.1:") and "?token=" in srv.url
        base = srv.url.split("/?")[0]
        code, page = _req(srv.url)
        assert code == 200 and "<title>delege settings</title>" in page and srv.token in page
        assert "Create a key…" in page and "Policy" in page and "Ollama" in page
        assert _req(base + "/")[0] == 403 and _req(base + "/?token=wrong")[0] == 403
        assert _req(base + "/api/state")[0] == 403
        code, body = _req(base + "/api/state", token=srv.token)
        assert code == 200 and json.loads(body)["ollama"]["models"] == ["q"]
        assert _req(base + "/api/state", token=srv.token, host="evil.example")[0] == 403    # DNS rebinding
        code, body = _req(base + "/api/key", token=srv.token, body={"provider": "groq", "key": "k"})
        assert code == 200 and json.loads(body) == {"where": "secrets"}
        assert cloud.key_for("groq") == ("k", "secrets")
        code, body = _req(base + "/api/test", token=srv.token, body={"id": "groq/openai/gpt-oss-20b"})
        assert json.loads(body) == {"ok": True, "message": "fake"}
        code, body = _req(base + "/api/cloud", token=srv.token, body={"on": True})
        assert code == 200 and cloud.load_settings()["cloud"]
        assert _req(base + "/api/key", token=srv.token, body={"provider": "zz", "key": "k"})[0] == 400
        assert _req(base + "/api/nope", token=srv.token, body={})[0] == 400
        assert _req(base + "/api/key", body={"provider": "groq", "key": "k2"})[0] == 403
        assert cloud.key_for("groq") == ("k", "secrets")
    finally:
        srv.close()


def test_migrate_fifoo_files_once(tmp_path, monkeypatch):
    from delege.migrate import migrate_once
    old = tmp_path / "fifoo"
    (old / "delegate").mkdir(parents=True)
    monkeypatch.setenv("FIFOO_HOME", str(old))
    assert migrate_once() == []                                       # nothing to migrate
    (old / "delegate.json").write_text(json.dumps({
        "hosts": [{"name": "rtx", "slots": 1, "gpu_gb": 32, "repo": "/home/jm/FiFoo"},
                  {"name": "box2", "ssh": "jm@box2", "repo": "~/FiFoo", "agents": ["docs"]}],
        "identity": {"name": "JM", "email": "jm@x.org"}, "parallel": 2, "dataset_dirs": ["data/x"],
        "models": [{"id": "groq/openai/gpt-oss-20b", "limits": {"rpd": 10}}]}))
    (old / "cloud.json").write_text(json.dumps({"cloud": True, "models": {"google/gemini-2.5-flash": True}}))
    (old / "secrets.json").write_text(json.dumps({"groq": "gsk_old"}))
    (old / "delegate" / "usage.json").write_text(json.dumps({"benched": {}}))
    lines = migrate_once()
    assert lines and "migrated" in lines[0] and any("dataset_dirs" in ln for ln in lines)
    cfg = json.loads(paths.config_file().read_text())
    assert cfg["hosts"] == [{"name": "rtx", "slots": 1, "gpu_gb": 32},
                            {"name": "box2", "ssh": "jm@box2", "agents": ["docs"], "repos": {"jcherfils/FiFoo": "~/FiFoo"}}]
    assert cfg["identity"] == {"name": "JM", "email": "jm@x.org"} and cfg["parallel"] == 2
    assert cfg["cloud"] is True and cfg["enabled"] == {"google/gemini-2.5-flash": True}
    assert cfg["models"][0]["id"] == "groq/openai/gpt-oss-20b"
    assert cloud.key_for("groq") == ("gsk_old", "secrets")
    assert paths.usage_file().exists()
    assert (old / "secrets.json").exists()                            # the old files are left alone
    assert migrate_once() == []                                       # only once


def test_check_images(tmp_path):
    from PIL import Image
    import random
    from delege.images import check_images
    rnd = random.Random(0)
    good = tmp_path / "overview.png"
    im = Image.new("RGB", (640, 400))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(640 * 400)])
    im.save(good)
    blank = tmp_path / "nodes.png"
    Image.new("RGB", (640, 400), (0, 0, 0)).save(blank)
    small = tmp_path / "corridor.png"
    Image.new("RGB", (160, 100), (10, 20, 30)).save(small)
    bad = check_images([good, blank, small, tmp_path / "missing.png"])
    assert any("nodes.png: blank" in b for b in bad) and any("160×100" in b for b in bad)
    assert any("missing.png: unreadable" in b for b in bad) and not any("overview" in b for b in bad)
    from delege.cli import main
    assert main(["check-images", str(good)]) == 0 and main(["check-images", str(blank)]) == 1
