import os
import time
import inspect
import requests
import logging
import contextvars
from collections import OrderedDict
from openai import OpenAI, AzureOpenAI

from app.llm_pricing import log_llm_metrics_db
from app.llm_utils import strip_reasoning_and_think_tags

logger = logging.getLogger(__name__)

# Dynamic tenant context variable
current_client_id = contextvars.ContextVar("current_client_id", default="SYSTEM")

_llm_configs_cache = {}
_LLM_CONFIGS_CACHE_TTL = 30  # seconds

_DYNAMIC_CLIENT_MAX_SIZE = 50
_DYNAMIC_CLIENT_TTL = 300  # 5 minutes
_dynamic_clients_cache = OrderedDict()
_dynamic_clients_ts = {}


def get_llm_config(config_id: int) -> dict | None:
    now = time.time()
    cached = _llm_configs_cache.get(config_id)
    if cached and now - cached[1] < _LLM_CONFIGS_CACHE_TTL:
        return cached[0]
    
    config = None
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT provider, api_key, base_url, model_name, api_version, name FROM globally_available_llm_configs WHERE id=%s",
                    (config_id,)
                )
                row = cursor.fetchone()
                if row:
                    from app.secrets_crypto import decrypt_secret
                    raw_key = row[1]
                    try:
                        api_key = decrypt_secret(raw_key) if raw_key and raw_key.startswith("gAAAAA") else raw_key
                    except Exception:
                        api_key = raw_key
                    config = {
                        "provider": row[0],
                        "api_key": api_key,
                        "base_url": row[2],
                        "model_name": row[3],
                        "api_version": row[4],
                        "name": row[5]
                    }
    except Exception as e:
        logger.warning(f"Failed to fetch globally available llm config {config_id}: {e}")
    
    if config:
        _llm_configs_cache[config_id] = (config, now)
    return config


def get_llm_config_for_client(client_id: str, caller_function: str) -> dict:
    """
    Dynamically resolves the LLM Configuration dict to use for the given client_id and caller_function.
    Queries client_llm_config -> globally_available_llm_configs -> global_default_llm -> env vars.
    """
    from app.db import get_db_ctx

    # 0. Emergency Global Override Check: If is_override_active is True, ALL callers and clients MUST use global_default_llm
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, model_name, api_version, is_override_active 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                g_row = cursor.fetchone()
                if not g_row:
                    cursor.execute("SELECT id, provider, api_key, base_url, model_name, api_version, is_override_active FROM global_default_llm LIMIT 1")
                    g_row = cursor.fetchone()
                if g_row and len(g_row) > 6 and g_row[6]:  # is_override_active is True
                    logger.warning(f"🚨 EMERGENCY GLOBAL OVERRIDE ACTIVE: Enforcing Global Default LLM for client={client_id}, function={caller_function}")
                    from app.secrets_crypto import decrypt_secret
                    raw_g_key = g_row[2]
                    try:
                        g_key = decrypt_secret(raw_g_key) if raw_g_key and raw_g_key.startswith("gAAAAA") else raw_g_key
                    except Exception:
                        g_key = raw_g_key
                    return {
                        "id": g_row[0],
                        "provider": g_row[1],
                        "api_key": g_key,
                        "base_url": g_row[3],
                        "model_name": g_row[4],
                        "api_version": g_row[5],
                        "name": f"EMERGENCY GLOBAL OVERRIDE ({g_row[1].upper()})"
                    }
    except Exception as e:
        logger.warning(f"Error checking emergency global override in get_llm_config_for_client: {e}")

    # 1. Check for specific override / custom config in client_llm_config table
    override_model = None
    if client_id and client_id != "SYSTEM":
        try:
            with get_db_ctx() as db:
                with db.cursor() as cursor:
                    cursor.execute("""
                        SELECT model_name, global_config_id, provider, api_key, base_url, api_version 
                        FROM client_llm_config 
                        WHERE client_id=%s AND caller_function=%s
                    """, (client_id, caller_function))
                    row = cursor.fetchone()
                    if row:
                        c_model, c_global_id, c_provider, c_key, c_url, c_ver = row
                        
                        # 1a. If client specified full custom provider credentials for this function
                        if c_provider and c_key:
                            from app.secrets_crypto import decrypt_secret
                            try:
                                dec_c_key = decrypt_secret(c_key, client_id=client_id) if c_key and c_key.startswith("gAAAAA") else c_key
                            except Exception:
                                dec_c_key = c_key
                            return {
                                "provider": c_provider,
                                "api_key": dec_c_key,
                                "base_url": c_url,
                                "model_name": c_model,
                                "api_version": c_ver,
                                "name": f"Client {client_id} Custom ({c_provider})"
                            }

                        # 1b. If referencing a specific globally available config ID
                        if c_global_id:
                            global_cfg = get_llm_config(c_global_id)
                            if global_cfg:
                                cfg_copy = dict(global_cfg)
                                if c_model and c_model != f"config_{c_global_id}":
                                    cfg_copy["model_name"] = c_model
                                return cfg_copy

                        # 1c. If model_name specifies config_<id>
                        if c_model and c_model.startswith("config_"):
                            try:
                                cfg_id = int(c_model.split("_")[1])
                                global_cfg = get_llm_config(cfg_id)
                                if global_cfg:
                                    return global_cfg
                            except Exception:
                                pass

                        # 1d. If model_name is a direct model string
                        if c_model:
                            override_model = c_model
        except Exception as e:
            logger.warning(f"Error checking client_llm_config override: {e}")

    resolved_config = None

    # 2. Fall back to the Global Default (global_default_llm table, id=1)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, model_name, api_version 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    cursor.execute("SELECT id, provider, api_key, base_url, model_name, api_version FROM global_default_llm LIMIT 1")
                    row = cursor.fetchone()
                if row:
                    from app.secrets_crypto import decrypt_secret
                    raw_row_key = row[2]
                    try:
                        row_key = decrypt_secret(raw_row_key) if raw_row_key and raw_row_key.startswith("gAAAAA") else raw_row_key
                    except Exception:
                        row_key = raw_row_key
                    resolved_config = {
                        "id": row[0],
                        "provider": row[1],
                        "api_key": row_key,
                        "base_url": row[3],
                        "model_name": row[4],
                        "api_version": row[5],
                        "name": f"Global Default ({row[1].upper()})"
                    }
    except Exception as e:
        logger.warning(f"Error fetching global_default_llm: {e}")

    # 3. Fall back to ANY available configuration in globally_available_llm_configs table if no global_default_llm
    if not resolved_config:
        try:
            with get_db_ctx() as db:
                with db.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, provider, api_key, base_url, model_name, api_version, name FROM globally_available_llm_configs ORDER BY id ASC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    if row:
                        from app.secrets_crypto import decrypt_secret
                        raw_fb_key = row[2]
                        try:
                            fb_key = decrypt_secret(raw_fb_key) if raw_fb_key and raw_fb_key.startswith("gAAAAA") else raw_fb_key
                        except Exception:
                            fb_key = raw_fb_key
                        resolved_config = {
                            "id": row[0],
                            "provider": row[1],
                            "api_key": fb_key,
                            "base_url": row[3],
                            "model_name": row[4],
                            "api_version": row[5],
                            "name": row[6]
                        }
        except Exception as e:
            logger.warning(f"Error fetching fallback from globally_available_llm_configs: {e}")

    # 4. If an override model was defined in client_llm_config, apply it
    if resolved_config and override_model:
        resolved_config = dict(resolved_config)
        resolved_config["model_name"] = override_model

    # 5. Ultimate fallback to environment variables
    if not resolved_config:
        default_groq_key = os.getenv("GROQ_API_KEY", "").strip()
        default_groq_model = os.getenv("GROQ_MODEL", "").strip()
        resolved_config = {
            "provider": "groq",
            "api_key": default_groq_key,
            "base_url": "https://api.groq.com/openai/v1",
            "model_name": override_model or default_groq_model,
            "api_version": None,
            "name": "System Env Default (Groq)"
        }

    return resolved_config


class AnthropicCompletionsAdapter:
    def __init__(self, api_key: str, base_url: str = None):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")

    def create(self, *args, **kwargs):
        model = kwargs.get("model", "claude-3-5-sonnet-20241022")
        messages = kwargs.get("messages", [])
        temperature = kwargs.get("temperature", 0.2)
        max_tokens = kwargs.get("max_tokens", 4096)

        system_prompt = ""
        claude_messages = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_prompt += (content + "\n\n")
            elif role in ("user", "assistant"):
                claude_messages.append({"role": role, "content": content})

        if not claude_messages:
            claude_messages = [{"role": "user", "content": "Please proceed."}]

        # Ensure alternating user/assistant messages for Claude API
        normalized_messages = []
        for msg in claude_messages:
            if normalized_messages and normalized_messages[-1]["role"] == msg["role"]:
                normalized_messages[-1]["content"] += ("\n\n" + msg["content"])
            else:
                normalized_messages.append(dict(msg))

        if normalized_messages and normalized_messages[0]["role"] != "user":
            normalized_messages.insert(0, {"role": "user", "content": "Please proceed."})

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": normalized_messages,
            "temperature": temperature
        }
        if system_prompt.strip():
            payload["system"] = system_prompt.strip()

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        resp = requests.post(f"{self.base_url}/messages", json=payload, headers=headers, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")

        res_json = resp.json()
        content_text = ""
        for block in res_json.get("content", []):
            if block.get("type") == "text":
                content_text += block.get("text", "")

        usage = res_json.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)

        class _Msg:
            def __init__(self, c):
                self.content = c
                self.role = "assistant"
        class _Choice:
            def __init__(self, m):
                self.message = m
        class _Usage:
            def __init__(self, p, c):
                self.prompt_tokens = p
                self.completion_tokens = c
                self.total_tokens = p + c
        class _Res:
            def __init__(self, text, pt, ct):
                self.choices = [_Choice(_Msg(text))]
                self.usage = _Usage(pt, ct)

        return _Res(content_text, prompt_tokens, completion_tokens)


class AnthropicChatAdapter:
    def __init__(self, api_key: str, base_url: str = None):
        self.completions = AnthropicCompletionsAdapter(api_key, base_url)


class AnthropicClientAdapter:
    def __init__(self, api_key: str, base_url: str = None):
        self.chat = AnthropicChatAdapter(api_key, base_url)


def _build_client(config: dict):
    provider = config["provider"].lower()
    api_key = config["api_key"]
    base_url = config["base_url"]
    api_version = config["api_version"]
    
    # Resolve default base URL if not explicitly provided
    if not base_url:
        if provider == "groq":
            base_url = "https://api.groq.com/openai/v1"
        elif provider == "openai":
            base_url = "https://api.openai.com/v1"
        elif provider in ("claude", "anthropic"):
            base_url = "https://api.anthropic.com/v1"
        elif provider == "grok":
            base_url = "https://api.x.ai/v1"
        elif provider == "gemini":
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    
    if provider in ("claude", "anthropic"):
        return AnthropicClientAdapter(api_key=api_key, base_url=base_url)
    elif provider == "azure":
        return AzureOpenAI(
            api_key=api_key,
            api_version=api_version or "2024-02-15-preview",
            azure_endpoint=base_url
        )
    else:
        return OpenAI(
            api_key=api_key,
            base_url=base_url or None
        )


def get_dynamic_client(config_id: int, config: dict):
    """
    Returns a cached dynamic client or creates a new one.
    Implements TTL (5 min) and LRU eviction (max 50 entries) to prevent unbounded memory growth.
    """
    cache_key = (
        config_id,
        config["provider"],
        config["api_key"],
        config["base_url"],
        config["api_version"]
    )
    now = time.time()

    # Check TTL
    if cache_key in _dynamic_clients_cache:
        if now - _dynamic_clients_ts.get(cache_key, 0) < _DYNAMIC_CLIENT_TTL:
            _dynamic_clients_cache.move_to_end(cache_key)
            return _dynamic_clients_cache[cache_key]
        else:
            del _dynamic_clients_cache[cache_key]
            _dynamic_clients_ts.pop(cache_key, None)

    # Evict oldest if at capacity
    while len(_dynamic_clients_cache) >= _DYNAMIC_CLIENT_MAX_SIZE:
        evicted_key, _ = _dynamic_clients_cache.popitem(last=False)
        _dynamic_clients_ts.pop(evicted_key, None)

    new_client = _build_client(config)
    _dynamic_clients_cache[cache_key] = new_client
    _dynamic_clients_ts[cache_key] = now
    return new_client


def telemetry_create(*args, caller: str | None = None, **kwargs):
    if not caller:
        caller = "unknown"
        try:
            stack = inspect.stack()
            if len(stack) > 1:
                for frame in stack[1:4]:
                    fn = frame.function
                    if fn not in ("create", "telemetry_create"):
                        caller = fn
                        break
        except Exception:
            pass

    start_time = time.time()
    client_id = current_client_id.get()

    try:
        config = get_llm_config_for_client(client_id, caller)
        actual_model = kwargs.get("model") or config["model_name"]
        kwargs["model"] = actual_model
        provider = config.get("provider", "groq").lower()

        # Remove raw reasoning_effort passed from legacy call sites
        kwargs.pop("reasoning_effort", None)

        # REASONING SUPPRESSION / MULTI-PROVIDER TWEAKS
        if provider == "gemini":
            extra = kwargs.get("extra_body", {})
            extra["thinking_config"] = {"thinking_budget": 0}
            kwargs["extra_body"] = extra
        elif provider == "openai":
            if actual_model.startswith("o1") or actual_model.startswith("o3"):
                kwargs.pop("temperature", None)
                if "max_tokens" in kwargs:
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                kwargs["reasoning_effort"] = "low"
        elif provider == "groq":
            actual_lower = actual_model.lower()
            if any(r in actual_lower for r in ["r1", "qwq", "reasoning", "deepseek-r1"]):
                extra = kwargs.get("extra_body", {})
                extra["reasoning_format"] = "parsed"
                kwargs["extra_body"] = extra

        # Circuit breaker check
        from app.llm_circuit_breaker import llm_circuit_breaker
        if llm_circuit_breaker.is_open(provider):
            raise RuntimeError(f"🔌 Circuit breaker is OPEN for provider '{provider}' — failing fast")

        target_client = get_dynamic_client(config.get("id", 0), config)
    except Exception as exc:
        logger.error(f"❌ LLM Config Resolution Failed: {exc}")
        raise exc

    try:
        res = target_client.chat.completions.create(*args, **kwargs)
        llm_circuit_breaker.record_success(provider)
    except Exception as call_err:
        llm_circuit_breaker.record_failure(provider)
        raise call_err

    latency_ms = (time.time() - start_time) * 1000

    try:
        prompt_tokens = 0
        completion_tokens = 0
        
        # 1. Standard OpenAI / Groq / Azure / xAI / DeepSeek object
        if res and hasattr(res, "usage") and res.usage:
            prompt_tokens = getattr(res.usage, "prompt_tokens", 0) or getattr(res.usage, "input_tokens", 0) or 0
            completion_tokens = getattr(res.usage, "completion_tokens", 0) or getattr(res.usage, "output_tokens", 0) or 0
        # 2. Dictionary format
        elif isinstance(res, dict) and "usage" in res and res["usage"]:
            u = res["usage"]
            prompt_tokens = u.get("prompt_tokens") or u.get("input_tokens") or 0
            completion_tokens = u.get("completion_tokens") or u.get("output_tokens") or 0
        # 3. Gemini usage_metadata
        elif res and hasattr(res, "usage_metadata") and res.usage_metadata:
            prompt_tokens = getattr(res.usage_metadata, "prompt_token_count", 0) or 0
            completion_tokens = getattr(res.usage_metadata, "candidates_token_count", 0) or 0

        # Heuristic fallback if provider returned 0 or None
        if prompt_tokens == 0:
            msgs = kwargs.get("messages", [])
            raw_text = " ".join([m.get("content", "") for m in msgs if isinstance(m, dict)])
            if raw_text:
                prompt_tokens = max(1, int(len(raw_text) / 4))
        if completion_tokens == 0 and res and hasattr(res, "choices") and res.choices:
            for choice in res.choices:
                if hasattr(choice, "message") and hasattr(choice.message, "content") and choice.message.content:
                    completion_tokens += max(1, int(len(choice.message.content) / 4))

        # Universal reasoning and thinking tag suppression on choices
        if res and hasattr(res, "choices") and res.choices:
            for choice in res.choices:
                if hasattr(choice, "message") and hasattr(choice.message, "content"):
                    raw_c = choice.message.content
                    if raw_c:
                        choice.message.content = strip_reasoning_and_think_tags(raw_c)

        log_llm_metrics_db(client_id, provider, actual_model, prompt_tokens, completion_tokens, latency_ms, caller)
    except Exception as telemetry_err:
        logger.warning(f"Telemetry tracking error: {telemetry_err}")

    return res

class _TelemetryCompletions:
    def create(self, *args, caller: str | None = None, **kwargs):
        return telemetry_create(*args, caller=caller, **kwargs)


class _TelemetryChat:
    def __init__(self):
        self.completions = _TelemetryCompletions()


class TelemetryLLMClient:
    """
    Transparent client wrapper providing the OpenAI-compatible client.chat.completions.create(...)
    interface while dynamically resolving tenant configurations, managing circuit breakers,
    and recording cost telemetry without monkey-patching external libraries.
    """
    def __init__(self):
        self.chat = _TelemetryChat()


# Canonical shared client instance
client = TelemetryLLMClient()


def resolve_langchain_model(client_id: str, caller_function: str, temperature: float = 0.2):
    """
    Resolves the LLM config for LangChain usage, returns a ChatOpenAI, AzureChatOpenAI,
    or ChatAnthropic instance configured with database credentials.
    """
    config = get_llm_config_for_client(client_id, caller_function)
    provider = config["provider"].lower()
    
    base_url = config["base_url"]
    if not base_url:
        if provider == "groq":
            base_url = "https://api.groq.com/openai/v1"
        elif provider == "openai":
            base_url = "https://api.openai.com/v1"
        elif provider == "grok":
            base_url = "https://api.x.ai/v1"
        elif provider == "gemini":
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        elif provider in ("claude", "anthropic"):
            base_url = "https://api.anthropic.com/v1"
            
    if provider in ("claude", "anthropic"):
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model_name=config["model_name"],
                anthropic_api_key=config["api_key"],
                temperature=temperature
            )
        except Exception:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=config["model_name"],
                openai_api_key=config["api_key"],
                openai_api_base=base_url or None,
                temperature=temperature
            )
    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            model=config["model_name"],
            openai_api_key=config["api_key"],
            openai_api_version=config["api_version"] or "2024-02-15-preview",
            azure_endpoint=base_url,
            temperature=temperature
        )
    else:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=config["model_name"],
            openai_api_key=config["api_key"],
            openai_api_base=base_url or None,
            temperature=temperature
        )


_model_cache = {}
_MODEL_CACHE_TTL = 60  # seconds

def resolve_model(client_id: str, caller_function: str) -> str:
    cache_key = (client_id, caller_function)
    now = time.time()
    cached = _model_cache.get(cache_key)
    if cached and now - cached[1] < _MODEL_CACHE_TTL:
        return cached[0]
    
    try:
        config = get_llm_config_for_client(client_id, caller_function)
        model = config["model_name"]
    except Exception as e:
        logger.warning(f"resolve_model lookup failed, using fallback: {e}")
        model = "llama-3.3-70b-versatile"
        
    _model_cache[cache_key] = (model, now)
    return model
