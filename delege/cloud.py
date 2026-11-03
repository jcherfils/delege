"""Hosted models with a **free tier**: the registry (providers, models, free limits, data policies), where their
API keys live, the switch and the per-model on / off, the daily usage count, and a small OpenAI-compatible chat
client (the *Test* probe, and any tool that wants to ask these models a question).

Only free tiers are listed: a model without a declared free limit is never used. The registry is a snapshot of the
providers' pages read on 2026-11-02 (links in the README): limits change; the daily count and the 429 answers are
what protects the free tier. ``config.json`` › ``"models"`` adjusts it without touching the code.

Keys: the provider's environment variable, else the OS keyring (``keyring`` package, optional, service
``delege``), else ``~/.config/delege/secrets.json`` (mode 0600), else OpenCode's own store
(``~/.local/share/opencode/auth.json``, read only). Never in a repository. Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths
from .util import read_json, warn_once, write_json

KEYRING_SERVICE = "delege"
LEGACY_KEYRING_SERVICE = "fifoo-cloud"                  # FiFoo's: read once by the migration
PROBE_TIMEOUT = 30.0
RATE_LIMIT_WORDS = re.compile(r"rate.?limit|quota|too many requests|resource.?exhausted|429|insufficient|exceeded", re.I)


@dataclass(frozen=True)
class Limits:
    """A free tier's limits, as published (``source``) or as a budget this tool imposes when the provider publishes
    none (``source`` says so). ``None``: not limited on that axis."""
    rpm: int | None = None            # requests per minute
    rpd: int | None = None            # requests per day
    tpm: int | None = None            # tokens per minute
    tpd: int | None = None            # tokens per day
    source: str = ""                  # where the figures come from (a sentence)

    def text(self) -> str:
        parts = [f"{v} {k}" for k, v in (("req/min", self.rpm), ("req/day", self.rpd), ("tok/min", self.tpm),
                                           ("tok/day", self.tpd)) if v]
        return ", ".join(parts) or "no figure published"


@dataclass(frozen=True)
class Provider:
    id: str                           # OpenCode's provider id (the first part of ``provider/model``)
    name: str
    base_url: str                     # OpenAI-compatible endpoint (``/chat/completions`` appended)
    key_env: str | None               # the environment variable OpenCode and this module read
    key_url: str | None               # where the owner creates a key
    policy: str                       # data use on the free tier, one line
    policy_url: str                   # the page that says so
    limits: Limits = Limits()         # limits shared by all the provider's free models (OpenRouter's daily count)
    local: bool = False
    free_url: str = ""                # the page describing the free tier
    note: str = ""


@dataclass(frozen=True)
class Model:
    id: str                           # ``provider/model`` as OpenCode names it
    name: str
    limits: Limits
    strength: int = 2                 # 1 small, 2 good coder, 3 the strongest (fix / checks go there first)
    vision: bool = False              # takes images
    trains: bool = False              # the provider may train on or log the prompts: off unless the owner says so
    policy: str = ""                  # the model's own data policy when it differs from its provider's
    context: int = 128000

    @property
    def provider(self) -> str:
        return self.id.split("/", 1)[0]

    @property
    def local(self) -> bool:
        p = PROVIDERS.get(self.provider)
        return bool(p and p.local)

    @property
    def free(self) -> bool:
        """Declared free: a limit on at least one axis (a model without one is never used)."""
        return self.local or any((self.limits.rpm, self.limits.rpd, self.limits.tpm, self.limits.tpd))


_ZEN = Limits(rpd=200, tpd=2_000_000, source="budget: OpenCode publishes no figure for its free models")
_GEMINI = Limits(rpm=10, rpd=250, tpm=250_000, source="budget: Google shows the free figures only in AI Studio › "
                                                     "rate limits (per project, reset at midnight Pacific)")
_MISTRAL = Limits(rpm=30, rpd=300, tpd=1_000_000, source="budget: Mistral shows Free-mode figures only in its "
                                                        "admin panel › Limits")

PROVIDERS: dict[str, Provider] = {
    "ollama": Provider("ollama", "Ollama (local)", "http://localhost:11434/v1", None, None,
                       "nothing leaves this machine", "https://docs.ollama.com/faq", local=True),
    "opencode": Provider(
        "opencode", "OpenCode (free models)", "https://opencode.ai/inference/openai/v1", "OPENCODE_API_KEY",
        "https://opencode.ai/auth",
        "zero retention, no training for GLM-5.3-Flash and DeepSeek V4.1 Flash; Big Pickle / MiMo / Flint may use "
        "the data to improve the model; Beam keeps it 30 days; Nemotron (NVIDIA) logs it — 'trial use only'",
        "https://opencode.ai/docs/zen", free_url="https://opencode.ai/docs/zen",
        note="free 'for a limited time'; usable from OpenCode only; OPENCODE_DISABLE_FREE_MODELS=1 hides them"),
    "openrouter": Provider(
        "openrouter", "OpenRouter (:free models)", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        "https://openrouter.ai/settings/keys",
        "OpenRouter stores no prompt unless you opt in; each :free model's upstream provider sets its own "
        "(Google: no training, kept 55 days; Cohere: no training, kept 30 days; Poolside: no training; "
        "NVIDIA / Thinking Machines / Liquid: may train — not listed here)",
        "https://openrouter.ai/docs/guides/privacy/logging",
        limits=Limits(rpm=20, rpd=50, source="OpenRouter › API limits: 20 req/min, 50 req/day for all :free models "
                                              "together (1000/day once $10 of credits were ever bought)"),
        free_url="https://openrouter.ai/docs/guides/routing/model-variants/free"),
    "google": Provider(
        "google", "Google AI Studio (Gemini, free tier)",
        "https://generativelanguage.googleapis.com/v1beta/openai", "GOOGLE_GENERATIVE_AI_API_KEY",
        "https://aistudio.google.com/apikey",
        "free tier: prompts and answers may be used to improve Google's products and read by reviewers — except "
        "for users in the EEA, Switzerland and the UK, who get the paid-service terms (no such use)",
        "https://ai.google.dev/gemini-api/terms", free_url="https://ai.google.dev/gemini-api/docs/rate-limits"),
    "groq": Provider(
        "groq", "Groq (free plan)", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        "https://console.groq.com/keys",
        "no training on inputs or outputs; no retention of inference data by default (logs up to 30 days; a "
        "zero-data-retention switch in Data Controls)",
        "https://console.groq.com/docs/your-data", free_url="https://console.groq.com/docs/rate-limits"),
    "mistral": Provider(
        "mistral", "Mistral (Free mode)", "https://api.mistral.ai/v1", "MISTRAL_API_KEY",
        "https://console.mistral.ai/api-keys",
        "Free mode trains on inputs and outputs by default: opt out first in the admin panel › Privacy › "
        "'Anonymous improvement data' (the API toggle), then enable these models",
        "https://help.mistral.ai/en/articles/455207-can-i-opt-out-of-my-input-or-output-data-being-used-for-training",
        free_url="https://docs.mistral.ai/admin/billing-usage/usage-limits"),
}

MODELS: dict[str, Model] = {m.id: m for m in (
    Model("ollama/qwen3:14b", "Qwen3 14B (local)", Limits(source="local: no limit"), strength=1, context=40960),
    Model("ollama/qwen3-coder:30b", "Qwen3-Coder 30B-A3B (local)", Limits(source="local: no limit"), strength=2,
          context=65536),
    # OpenCode's free models (https://opencode.ai/docs/zen, 2026-11-02)
    Model("opencode/glm-5.3-flash-free", "GLM-5.3-Flash (free)", _ZEN, strength=3,
          policy="zero retention, no training"),
    Model("opencode/deepseek-v4.1-flash-free", "DeepSeek V4.1 Flash (free)", _ZEN, strength=3,
          policy="zero retention, no training"),
    Model("opencode/beam-free", "Beam (free)", _ZEN, strength=2, policy="no training; kept 30 days for safety"),
    Model("opencode/big-pickle", "Big Pickle (stealth, free)", _ZEN, strength=2, trains=True,
          policy="collected data may be used to improve the model during the free period"),
    Model("opencode/mimo-v2.6-flash-free", "MiMo-V2.6-Flash (free)", _ZEN, strength=2, trains=True,
          policy="collected data may be used to improve the model during the free period"),
    Model("opencode/flint-40b-free", "Flint 40B (free)", _ZEN, strength=1, trains=True,
          policy="may be used to improve the model; kept 30 days"),
    Model("opencode/nemotron-3-ultra-free", "Nemotron 3 Ultra (free, NVIDIA)", _ZEN, strength=3, trains=True,
          policy="trial use only, logged, may improve NVIDIA products — no personal or confidential data"),
    Model("opencode/nemotron-3.5-super-vl-free", "Nemotron 3.5 Super VL (free, NVIDIA)", _ZEN, strength=2,
          vision=True, trains=True, policy="trial use only, logged, may improve NVIDIA products"),
    # OpenRouter :free models whose upstream does not train (2026-10-18 list; it changes often)
    Model("openrouter/google/gemma-4-31b-it:free", "Gemma 4 31B (OpenRouter, free)", Limits(rpm=20, rpd=50,
          source="OpenRouter's shared :free limits"), strength=2, vision=True, policy="Google AI Studio: no "
          "training, kept 55 days"),
    Model("openrouter/poolside/laguna-s-2.1:free", "Poolside Laguna S 2.1 (OpenRouter, free)", Limits(rpm=20, rpd=50,
          source="OpenRouter's shared :free limits"), strength=2, policy="Poolside: no training"),
    Model("openrouter/cohere/north-mini-code:free", "Cohere North Mini Code (OpenRouter, free)",
          Limits(rpm=20, rpd=50, source="OpenRouter's shared :free limits"), strength=2,
          policy="Cohere: no training, kept 30 days"),
    # Google AI Studio free tier (the figures are in AI Studio only; trains outside the EEA / UK / Switzerland)
    Model("google/gemini-2.5-flash", "Gemini 2.5 Flash (free tier)", _GEMINI, strength=3, vision=True, trains=True),
    Model("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite (free tier)", _GEMINI, strength=2, vision=True,
          trains=True),
    Model("google/gemini-2.5-pro", "Gemini 2.5 Pro (free tier)", Limits(rpm=5, rpd=100, tpm=250_000,
          source=_GEMINI.source), strength=3, vision=True, trains=True),
    # Groq free plan (console.groq.com/docs/rate-limits, per organisation)
    Model("groq/openai/gpt-oss-120b", "GPT-OSS 120B (Groq, free)", Limits(rpm=30, rpd=1000, tpm=8000, tpd=200_000,
          source="Groq › rate limits (free plan)"), strength=3),
    Model("groq/openai/gpt-oss-20b", "GPT-OSS 20B (Groq, free)", Limits(rpm=30, rpd=1000, tpm=8000, tpd=200_000,
          source="Groq › rate limits (free plan)"), strength=2),
    Model("groq/qwen/qwen3.8-27b", "Qwen 3.8 27B (Groq, free)", Limits(rpm=30, rpd=1000, tpm=8000, tpd=200_000,
          source="Groq › rate limits (free plan)"), strength=2),
    # Mistral Free mode (figures in the admin panel only; trains unless opted out)
    Model("mistral/devstral-small-latest", "Devstral Small (Mistral, Free mode)", _MISTRAL, strength=2, trains=True),
    Model("mistral/mistral-small-latest", "Mistral Small (Free mode)", _MISTRAL, strength=2, vision=True,
          trains=True),
)}

NOT_LISTED = {
    "Cerebras": "a one-off $5 trial that needs a card and expires after 30 days, not a standing free tier "
                "(https://inference-docs.cerebras.ai/support/rate-limits)",
    "GitHub Models": "retired on 2026-07-30 (https://docs.github.com/en/github-models)",
    "NVIDIA / Thinking Machines / Liquid on OpenRouter": "their :free endpoints may train on the prompts",
}


def model(model_id: str) -> Model:
    """The registry's entry, or a minimal one for an unknown id (no limit: never free, never used)."""
    if model_id in MODELS:
        return MODELS[model_id]
    return Model(model_id, model_id, Limits(source="unknown model: no declared free limit"))


def provider(model_id: str) -> Provider:
    pid = model_id.split("/", 1)[0]
    if pid not in PROVIDERS:
        return Provider(pid, pid, "", None, None, "unknown provider", "")
    return PROVIDERS[pid]


# ------------------------------------------------------------------ keys
def _keyring():
    try:
        import keyring                                    # optional (pip install delege[keyring])
        return keyring
    except ImportError:
        return None


def _secrets() -> dict:
    d = read_json(paths.secrets_file(), {})
    return d if isinstance(d, dict) else {}


def _write_secrets(d: dict):
    write_json(paths.secrets_file(), d, mode=0o600)
    os.chmod(paths.secrets_file(), 0o600)


def key_for(provider_id: str, environ: dict | None = None) -> tuple[str | None, str]:
    """The provider's API key and where it was found (``env``, ``keyring``, ``secrets``, ``opencode``), or
    ``(None, "")``. A local provider needs none: ``("", "local")``."""
    p = PROVIDERS.get(provider_id)
    if p is not None and p.local:
        return "", "local"
    env = os.environ if environ is None else environ
    if p is not None and p.key_env and env.get(p.key_env):
        return env[p.key_env], "env"
    kr = _keyring()
    if kr is not None:
        try:
            k = kr.get_password(KEYRING_SERVICE, provider_id)
            if k:
                return k, "keyring"
        except Exception as e:                            # noqa: BLE001  (no backend, locked: fall through)
            warn_once("cloud:keyring", "the OS keyring cannot be read, keys come from secrets.json", e)
    k = _secrets().get(provider_id)
    if isinstance(k, str) and k:
        return k, "secrets"
    auth = read_json(paths.opencode_auth(), {})
    e = auth.get(provider_id) if isinstance(auth, dict) else None
    if isinstance(e, dict) and isinstance(e.get("key"), str) and e["key"]:
        return e["key"], "opencode"
    return None, ""


def set_key(provider_id: str, key: str) -> str:
    """Stores (or, with an empty key, removes) the provider's key: the OS keyring when it works, else
    ``secrets.json`` (0600). Returns where it went."""
    key = key.strip()
    kr = _keyring()
    if kr is not None:
        try:
            if key:
                kr.set_password(KEYRING_SERVICE, provider_id, key)
            else:
                try:
                    kr.delete_password(KEYRING_SERVICE, provider_id)
                except kr.errors.PasswordDeleteError:
                    pass
            d = _secrets()
            if provider_id in d:                           # not in two places
                d.pop(provider_id)
                _write_secrets(d)
            return "keyring"
        except Exception as e:                            # noqa: BLE001  (no usable backend: the file instead)
            warn_once("cloud:keyring-set", "the OS keyring cannot be written, keys go to secrets.json", e)
    d = _secrets()
    if key:
        d[provider_id] = key
    else:
        d.pop(provider_id, None)
    _write_secrets(d)
    return "secrets"


def key_env(environ: dict | None = None) -> dict[str, str]:
    """The ``VAR=key`` pairs a subprocess (OpenCode) needs for every provider with a key, from the store — so a key
    kept in the keyring or ``secrets.json`` reaches OpenCode without being written anywhere else."""
    out = {}
    for p in PROVIDERS.values():
        if p.key_env:
            k, _where = key_for(p.id, environ)
            if k:
                out[p.key_env] = k
    return out


# ------------------------------------------------------------------ settings (the switch, on / off per model)
def load_settings() -> dict:
    """``{"cloud": bool, "models": {id: bool}}`` from ``config.json`` (keys ``cloud`` and ``enabled``)."""
    d = read_json(paths.config_file(), {})
    d = d if isinstance(d, dict) else {}
    models = d.get("enabled")
    return {"cloud": bool(d.get("cloud")), "models": dict(models) if isinstance(models, dict) else {}}


def save_settings(s: dict):
    """Writes the switch and the per-model on / off into ``config.json``, keeping its other keys."""
    d = read_json(paths.config_file(), {})
    d = d if isinstance(d, dict) else {}
    d["cloud"] = bool(s.get("cloud"))
    d["enabled"] = {str(k): bool(v) for k, v in (s.get("models") or {}).items()}
    write_json(paths.config_file(), d)


def enabled(m: Model, settings: dict | None = None) -> bool:
    """On unless switched off; a model whose provider may train on the prompts is off unless switched on."""
    s = settings if settings is not None else load_settings()
    v = s["models"].get(m.id)
    return bool(v) if v is not None else not m.trains


# ------------------------------------------------------------------ usage: requests and tokens per day
def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def next_reset(ts: float | None = None) -> float:
    """Midnight UTC after ``ts`` (the daily counts of these providers reset around then; Google's at midnight
    Pacific, which is later: the 429 answer then benches the model again)."""
    d = datetime.fromtimestamp(time.time() if ts is None else ts, UTC)
    return (d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


class Usage:
    """``usage.json``: per UTC day and model, the requests and tokens counted, and until when a model is benched
    (``benched``). One process writes at a time (a lock in memory; the file is rewritten whole, atomically)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.usage_file())
        self.lock = threading.Lock()
        self.data = read_json(self.path, {})
        if not isinstance(self.data, dict):
            self.data = {}

    def _day(self, model_id: str, day: str | None = None) -> dict:
        d = self.data.setdefault(day or today(), {})
        return d.setdefault(model_id, {"requests": 0, "tokens": 0})

    def save(self):
        keep = sorted(k for k in self.data if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k))[-7:]
        write_json(self.path, {k: self.data[k] for k in self.data if k in keep or k == "benched"})

    def record(self, model_id: str, requests: int = 1, tokens: int = 0):
        with self.lock:
            e = self._day(model_id)
            e["requests"] += int(requests)
            e["tokens"] += int(tokens)
            self.save()

    def counts(self, model_id: str) -> tuple[int, int]:
        e = self.data.get(today(), {}).get(model_id) or {}
        return int(e.get("requests", 0)), int(e.get("tokens", 0))

    def bench(self, model_id: str, until: float | None = None, why: str = ""):
        with self.lock:
            self.data.setdefault("benched", {})[model_id] = {"until": until or next_reset(), "why": why[:200]}
            self.save()

    def benched_until(self, model_id: str) -> float | None:
        e = (self.data.get("benched") or {}).get(model_id)
        if not e or float(e.get("until", 0)) <= time.time():
            return None
        return float(e["until"])

    def exhausted(self, m: Model) -> str | None:
        """Why the model should not be asked now: benched, its own daily count reached, or its provider's shared
        one; None when it may run."""
        until = self.benched_until(m.id)
        if until:
            return f"benched until {datetime.fromtimestamp(until, UTC).strftime('%H:%M UTC')}: " \
                   f"{(self.data.get('benched') or {}).get(m.id, {}).get('why', '')}".rstrip(": ")
        if not m.free:
            return "no declared free limit"
        req, tok = self.counts(m.id)
        if m.limits.rpd and req >= m.limits.rpd:
            return f"{req} requests today (free limit {m.limits.rpd}/day)"
        if m.limits.tpd and tok >= m.limits.tpd:
            return f"{tok} tokens today (free limit {m.limits.tpd}/day)"
        p = provider(m.id)
        if p.limits.rpd or p.limits.tpd:
            day = self.data.get(today(), {})
            preq = sum(int(e.get("requests", 0)) for k, e in day.items() if k.startswith(p.id + "/"))
            ptok = sum(int(e.get("tokens", 0)) for k, e in day.items() if k.startswith(p.id + "/"))
            if p.limits.rpd and preq >= p.limits.rpd:
                return f"{preq} requests today on {p.name} (shared free limit {p.limits.rpd}/day)"
            if p.limits.tpd and ptok >= p.limits.tpd:
                return f"{ptok} tokens today on {p.name} (shared free limit {p.limits.tpd}/day)"
        return None


def looks_rate_limited(text: str, status: int | None = None) -> bool:
    return status in (429, 402) or bool(RATE_LIMIT_WORDS.search(text or ""))


# ------------------------------------------------------------------ client
class CloudError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after

    @property
    def rate_limited(self) -> bool:
        return looks_rate_limited(str(self), self.status)


@dataclass
class Answer:
    text: str
    tokens: dict = field(default_factory=dict)        # input, output (as the provider counts them)
    model: str = ""
    raw: dict = field(default_factory=dict)


def _image_part(img) -> dict:
    """An image for the request: a path, bytes (JPEG / PNG) or an ``http(s)`` / ``data:`` URL."""
    if isinstance(img, (bytes, bytearray)):
        mime = "image/png" if bytes(img[:8]) == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        url = f"data:{mime};base64,{base64.b64encode(bytes(img)).decode()}"
    elif isinstance(img, str) and img.startswith(("http://", "https://", "data:")):
        url = img
    else:
        p = Path(img)
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        url = f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


def chat(model_id: str, prompt: str, images=(), *, system: str | None = None, max_tokens: int = 1024,
         key: str | None = None, timeout: float = 120.0, usage: Usage | None = None, base_url: str | None = None,
         opener=None) -> Answer:
    """One question to a hosted (or the local) model through the OpenAI-compatible ``/chat/completions``.
    ``images``: paths, bytes or URLs, only for a model flagged ``vision``. Counts the request in ``usage`` and
    refuses a model that is exhausted or has no declared free limit. Raises ``CloudError`` (``rate_limited`` on a
    429 / quota answer, which also benches the model until its reset or ``Retry-After``)."""
    m, p = model(model_id), provider(model_id)
    if not m.free:
        raise CloudError(f"{model_id}: no declared free limit — not used")
    if images and not m.vision:
        raise CloudError(f"{model_id} takes no image")
    usage = usage if usage is not None else Usage()
    why = usage.exhausted(m)
    if why:
        raise CloudError(f"{model_id}: {why}", status=429)
    if key is None:
        key, _where = key_for(p.id)
        if key is None:
            raise CloudError(f"{p.name}: no API key ({p.key_env}, or `delege settings`)")
    content = [{"type": "text", "text": prompt}] + [_image_part(i) for i in images] if images else prompt
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    body = {"model": model_id.split("/", 1)[1], "messages": msgs, "max_tokens": int(max_tokens), "stream": False}
    req = urllib.request.Request((base_url or p.base_url).rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                          "HTTP-Referer": "https://github.com/jcherfils/delege", "X-Title": "delege"})
    opener = opener or (urllib.request.build_opener(urllib.request.ProxyHandler({})) if p.local
                        else urllib.request.build_opener())
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:600]
        ra = e.headers.get("Retry-After") if e.headers else None
        err = CloudError(f"{model_id}: HTTP {e.code}: {text}", status=e.code,
                         retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if err.rate_limited:
            usage.bench(model_id, time.time() + err.retry_after if err.retry_after else next_reset(), f"HTTP {e.code}")
        raise err from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CloudError(f"{model_id}: {e}") from None
    u = raw.get("usage") or {}
    tokens = {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0)}
    usage.record(model_id, 1, tokens["input"] + tokens["output"])
    choices = raw.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    text = msg.get("content") or ""
    if isinstance(text, list):                        # some providers answer with content parts
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return Answer(text, tokens, raw.get("model") or model_id, raw)


def probe(model_id: str, **kw) -> tuple[bool, str]:
    """A 1-token question: ``(True, "answered in 0.8 s")`` or ``(False, why)`` — the settings page's *Test* and
    ``delege models``."""
    t0 = time.time()
    try:
        a = chat(model_id, "Say OK.", max_tokens=1, timeout=kw.pop("timeout", PROBE_TIMEOUT), **kw)
    except CloudError as e:
        return False, str(e)
    return True, f"answered in {time.time() - t0:.1f} s ({a.tokens.get('input', 0)} tokens in)"
