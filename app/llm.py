import os
import re
import json
import time
import inspect
import requests
import logging
import contextvars
from openai import OpenAI, AzureOpenAI
from pydantic import BaseModel
from typing import Literal

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Dynamic client holder
client = OpenAI(api_key="placeholder-unused")

current_client_id = contextvars.ContextVar("current_client_id", default="SYSTEM")

def strip_reasoning_and_think_tags(text: str) -> str:
    if not isinstance(text, str):
        return text
    # 1. Strip closed tags first
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'<thought>[\s\S]*?</thought>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'```thinking[\s\S]*?```', '', cleaned, flags=re.IGNORECASE)

    # 2. If closed tag didn't match and unclosed tag exists (e.g. truncated mid-thought)
    if '<think>' in cleaned.lower():
        # If there's an unclosed <think> at the start, strip it
        cleaned = re.sub(r'<think>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '<thought>' in cleaned.lower():
        cleaned = re.sub(r'<thought>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '<reasoning>' in cleaned.lower():
        cleaned = re.sub(r'<reasoning>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '```thinking' in cleaned.lower():
        cleaned = re.sub(r'```thinking[\s\S]*$', '', cleaned, flags=re.IGNORECASE)

    cleaned = cleaned.strip()
    
    # 3. If cleaning removed everything because the output was 100% truncated thinking process,
    # recover the most recent greeting/message drafted in the thought process
    if not cleaned and text:
        match = re.search(r'(Hi\s+[^\n]+,\s*[\s\S]+)', text, flags=re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
            # Clean any trailing thought markers
            cleaned = re.sub(r'</?think>.*$', '', cleaned, flags=re.IGNORECASE).strip()

    return cleaned.strip()

# Comprehensive Multi-Provider Model Pricing Registry (USD Per 1,000,000 Tokens: input, output)
PROVIDER_MODEL_PRICING = {
    # Anthropic / Claude
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),

    # OpenAI / Azure
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (1.10, 4.40),
    "o1-preview": (15.00, 60.00),
    "o3-mini": (1.10, 4.40),

    # Google Gemini
    "gemini-2.5-flash": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),

    # xAI Grok
    "grok-2": (2.00, 10.00),
    "grok-2-mini": (0.20, 1.00),
    "grok-beta": (5.00, 15.00),

    # DeepSeek
    "deepseek-chat": (0.14, 0.28),
    "deepseek-coder": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-r1": (0.55, 2.19),
    "deepseek-v3": (0.14, 0.28),

    # Groq (Hosted open source models)
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.3-70b-specdec": (0.59, 0.79),
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.2-1b-preview": (0.04, 0.04),
    "llama-3.2-3b-preview": (0.06, 0.06),
    "llama-3.2-11b-vision-preview": (0.18, 0.18),
    "llama-3.2-90b-vision-preview": (0.90, 0.90),
    "mixtral-8x7b-32768": (0.24, 0.24),
    "gemma2-9b-it": (0.20, 0.20),
    "qwen-2.5-32b": (0.20, 0.20),
    "qwen-2.5-72b": (0.40, 0.40),
    "qwen/qwen3-32b": (0.20, 0.20),
    "qwen/qwen3.6-27b": (0.18, 0.18),
}

DEFAULT_PROVIDER_PRICING = {
    "groq": (0.10, 0.20),
    "openai": (0.15, 0.60),
    "azure": (0.15, 0.60),
    "gemini": (0.10, 0.40),
    "anthropic": (3.00, 15.00),
    "claude": (3.00, 15.00),
    "grok": (2.00, 10.00),
    "deepseek": (0.20, 0.50),
    "ollama": (0.00, 0.00),      # Local self-hosted models = $0.00 API rate
    "custom": (0.10, 0.30),
}

def calculate_llm_cost(provider: str, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    provider_clean = (provider or "groq").lower().strip()
    model_clean = (model_name or "").lower().strip()

    input_rate = None
    output_rate = None

    for pattern, (in_p, out_p) in PROVIDER_MODEL_PRICING.items():
        if pattern in model_clean:
            input_rate = in_p
            output_rate = out_p
            break

    if input_rate is None or output_rate is None:
        def_in, def_out = DEFAULT_PROVIDER_PRICING.get(provider_clean, (0.15, 0.60))
        if "70b" in model_clean or "90b" in model_clean:
            input_rate, output_rate = 0.59, 0.79
        elif "8b" in model_clean or "7b" in model_clean or "1b" in model_clean or "3b" in model_clean:
            input_rate, output_rate = 0.05, 0.08
        elif "8x7b" in model_clean or "32b" in model_clean or "27b" in model_clean:
            input_rate, output_rate = 0.20, 0.20
        else:
            input_rate, output_rate = def_in, def_out

    cost = ((prompt_tokens * input_rate) + (completion_tokens * output_rate)) / 1_000_000.0
    return max(0.0, float(cost))

def log_llm_metrics_db(client_id: str, provider: str, model_name: str, prompt_tokens: int, completion_tokens: int, latency_ms: float, caller_function: str):
    provider_clean = (provider or "groq").lower().strip()
    cost = calculate_llm_cost(provider_clean, model_name, prompt_tokens, completion_tokens)

    multiplier = 1.0
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:

                cursor.execute("SELECT cost_multiplier FROM email_accounts WHERE client_id=%s", (client_id,))
                row = cursor.fetchone()
                if row and row[0] is not None:
                    multiplier = float(row[0])
                
                billed_cost = cost * multiplier
                cursor.execute("""
                    INSERT INTO llm_logs (client_id, provider, model_name, prompt_tokens, completion_tokens, cost, billed_cost, latency_ms, caller_function)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (client_id, provider_clean, model_name, prompt_tokens, completion_tokens, cost, billed_cost, int(latency_ms), caller_function))
            db.commit()
    except Exception as e:
        logger.warning(f"⚠️ Failed to write LLM telemetry log to database: {e}")

_llm_configs_cache = {}
_LLM_CONFIGS_CACHE_TTL = 30  # seconds
_dynamic_clients_cache = {}

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
                    config = {
                        "provider": row[0],
                        "api_key": row[1],
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
                    return {
                        "id": g_row[0],
                        "provider": g_row[1],
                        "api_key": g_row[2],
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
                            return {
                                "provider": c_provider,
                                "api_key": c_key,
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
                    resolved_config = {
                        "id": row[0],
                        "provider": row[1],
                        "api_key": row[2],
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
                        resolved_config = {
                            "id": row[0],
                            "provider": row[1],
                            "api_key": row[2],
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

def get_dynamic_client(config_id: int, config: dict):
    cache_key = (
        config_id,
        config["provider"],
        config["api_key"],
        config["base_url"],
        config["api_version"]
    )
    if cache_key in _dynamic_clients_cache:
        return _dynamic_clients_cache[cache_key]
    
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
        new_client = AnthropicClientAdapter(api_key=api_key, base_url=base_url)
    elif provider == "azure":
        new_client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version or "2024-02-15-preview",
            azure_endpoint=base_url
        )
    else:
        new_client = OpenAI(
            api_key=api_key,
            base_url=base_url or None
        )
        
    _dynamic_clients_cache[cache_key] = new_client
    return new_client

def telemetry_create(*args, **kwargs):
    caller = "unknown"
    try:
        stack = inspect.stack()
        if len(stack) > 1:
            caller = stack[1].function
    except Exception:
        pass

    start_time = time.time()
    client_id = current_client_id.get()

    try:
        config = get_llm_config_for_client(client_id, caller)
        actual_model = config["model_name"]
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

        target_client = get_dynamic_client(config.get("id", 0), config)
    except Exception as exc:
        logger.error(f"❌ LLM Config Resolution Failed: {exc}")
        raise exc

    res = target_client.chat.completions.create(*args, **kwargs)
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

client.chat.completions.create = telemetry_create

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
            # Fallback to OpenAI-compatible interface
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

# MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")


# ==============================
# 🔖 Conversational Brand Personas
# ==============================
AgentType = Literal[
    "customer_support",
    "ecommerce_support",
    "technical_support",
    "billing_support",
    "executive_escalation",
    "customer_support_agent",
    "ecommerce_support_agent",
    "crm_support_agent"
]

AGENT_PROMPTS = {
    # Primary Enterprise Personas
    "customer_support": "You are a professional, empathetic Customer Support Specialist dedicated to resolving user inquiries, answering product questions, and ensuring a delightful customer experience.",
    "ecommerce_support": "You are an E-Commerce & Logistics Support Specialist expert in order processing, parcel delivery status, returns, item exchanges, and refunds.",
    "technical_support": "You are a Technical Support & Solutions Engineer expert in diagnosing technical glitches, software/API troubleshooting, system configurations, and providing clear step-by-step guidance.",
    "billing_support": "You are a Billing & Invoicing Specialist handling subscription management, invoices, payment queries, renewals, and refunds with financial precision.",
    "executive_escalation": "You are an Executive Customer Success & Escalation Manager handling high-priority VIP customer matters, critical escalations, and sensitive account inquiries with utmost tact and dedication.",
    
    # Backward-compatible aliases
    "customer_support_agent": "You are a professional, empathetic Customer Support Specialist dedicated to resolving user inquiries, answering product questions, and ensuring a delightful customer experience.",
    "ecommerce_support_agent": "You are an E-Commerce & Logistics Support Specialist expert in order processing, parcel delivery status, returns, item exchanges, and refunds.",
    "crm_support_agent": "You are a Customer Relationship & Account Management Specialist dedicated to client communications, account follow-ups, and long-term partnership success."
}

TONE_INSTRUCTIONS = {
    "Formal": "Write in a Formal, polite, structured, and respectful business tone.",
    "Friendly": "Write in a Friendly, warm, conversational, and approachable tone that builds rapport.",
    "Concise": "Write in a Concise, direct, and high-efficiency tone. Deliver the answer in the fewest clear sentences possible without pleasantries or fluff.",
    "Empathetic": "Write in an Empathetic, reassuring, and patient tone. Acknowledge customer frustration with genuine care and provide clear reassurance.",
    "Technical": "Write in a Technical, precise, and analytical tone. Clearly explain root causes, parameters, technical steps, and actionable technical resolutions.",
    "Casual": "Write in a Casual, relaxed, and modern conversational tone while remaining helpful and clear."
}

class AgentRequest(BaseModel):
    agent_type: AgentType

def get_agent_prompt(request: AgentRequest) -> str:
    return AGENT_PROMPTS.get(request.agent_type, AGENT_PROMPTS["customer_support"])


# ==============================
# 👤 Extract Name from Email
# ==============================
def extract_name_from_email(email: str) -> str:
    try:
        username = email.split('@')[0]
        name_parts = re.split(r'[._\-]', username)
        formatted_name = ' '.join(
            [part.capitalize() for part in name_parts if part]
        )
        return formatted_name if formatted_name else "Customer"
    except Exception:
        return "Customer"


# ==============================
# 📜 Format history for prompt
# ==============================
def _format_history(history: list) -> str:
    if not history:
        return ""

    lines = ["--- Previous Conversation ---"]
    for entry in history:
        role_label = "Customer" if entry.get("role") == "customer" else "Support"
        ts      = entry.get("timestamp", "")[:16]
        subject = entry.get("subject", "")
        body    = entry.get("body", "")[:300]
        ticket  = entry.get("ticket_id", "")

        line = f"[{ts}] {role_label}"
        if ticket:
            line += f" (Ticket: {ticket})"
        line += f"\nSubject: {subject}\n{body}"
        lines.append(line)

    lines.append("--- End of History ---")
    return "\n\n".join(lines)


# ==============================
# 🎫 Ticket & Order ID Extractor
# ==============================
def extract_ticket_and_order_ids(text: str) -> list[str]:
    """
    Extracts all ticket IDs, order numbers, case numbers, and reference numbers
    from text using comprehensive regex patterns. Returns cleaned, deduplicated IDs.
    Also handles email line-wrapping/folding across newlines.
    """
    if not text:
        return []
    
    # Normalize soft line breaks within potential tokens (e.g., #27542400000039\r\n9001 -> #275424000000399001)
    texts_to_check = [text]
    unwrapped_text = re.sub(r'([A-Za-z0-9_#-]+)[\r\n]+([A-Za-z0-9_-]+)', r'\1\2', text)
    if unwrapped_text != text:
        texts_to_check.append(unwrapped_text)

    ids = []
    for t in texts_to_check:
        # 1. Standard pattern formats like T-YYMMDD-XXXXX
        for m in re.finditer(r'\b(T-\d{6}-\d+)\b', t, re.IGNORECASE):
            ids.append(m.group(1).upper())
        # 2. Common CRM / Ticketing prefixes (ORD, INC, CAS, SR, REQ)
        for m in re.finditer(r'\b(ORD-?\d+|INC\d+|CAS-\d+(?:-[A-Za-z0-9]+)?|SR-\d+|REQ\d+)\b', t, re.IGNORECASE):
            ids.append(m.group(1).upper())
        # 3. Explicit keywords: ticket/case/order/complaint/issue/ref followed by an ID
        for m in re.finditer(r'(?:ticket|case|order|complaint|issue|incident|ref(?:erence)?)\s*(?:id|no|num|number)?\s*[:#\s-]?\s*#?([A-Za-z0-9_-]{4,30})', t, re.IGNORECASE):
            val = m.group(1).strip()
            if val.lower() not in ("status", "update", "details", "information", "number", "issue", "query", "support", "please", "regarding", "about", "there", "here"):
                ids.append(val)
        # 4. Hash followed by digits/alphanumeric (e.g. #275424000000399001, #98765)
        for m in re.finditer(r'#([A-Za-z0-9_-]{4,30})', t):
            val = m.group(1).strip()
            if val:
                ids.append(val)

    clean_ids = []
    for item in ids:
        cleaned = item.strip().lstrip("#").strip()
        if cleaned and cleaned not in clean_ids:
            # If a longer version of this ID exists in clean_ids (e.g., 275424000000399001 vs 27542400000039), prefer the longer one
            if any(c != cleaned and cleaned in c for c in ids):
                continue
            clean_ids.append(cleaned)
    return clean_ids


# ==============================
# 🧠 Detect Intent
# ==============================
def detect_intent_llm(query: str) -> dict:
    prompt = f"""
You are a query classifier. Your only job is to analyze the user query and return structured JSON.

## Task
Classify the query into exactly one intent, extract ALL ticket_ids/order_ids if present, perform sentiment analysis, and assign a priority level.

## Intents
- `ticket_create`: User is explicitly asking to create, open, raise, or log a new ticket/complaint/case, or asking support to create a ticket for their issue
- `ticket_status`: User is asking about status of an existing ticket, order, complaint, delivery, or support request
- `marketing_promotional`: Marketing email, promotional campaign, newsletter, job alert blast, webinar invite, discount/sale offer, automated digest, or educational course advertisement (requiring no customer support action)
- `general_query`: Genuine customer support query, product question, policy inquiry, technical issue, or feedback requiring an answer

## Sentiment Analysis
Classify user sentiment into exactly one of:
- `Angry`: User shows frustration, anger, impatience, or threatens escalation/cancellation.
- `Neutral`: General query, factual, standard request, or marketing/newsletter announcement.
- `Happy`: Expresses gratitude, happiness, satisfaction.

## Priority Tagging
Classify priority level into exactly one of:
- `Critical`: Urgent issues like order cancellation, immediate refunds, lawsuit threats, legal actions, security/data issues, or extreme user anger.
- `High`: General support issues with angry/impatient sentiment, or containing key words like "urgent", "broken", "cancel", "refund", "sue", "failed".
- `Medium`: General query or ticket status checks with neutral sentiment.
- `Low`: Marketing/newsletter emails, promotional updates, positive feedback, or suggestions.

## Ticket & Order ID Extraction
Extract ALL ticket IDs, case numbers, order IDs, or tracking references mentioned in the query.
Examples:
- Numeric & Hash IDs: `#275424000000399001`, `275424000000399001`, `#98765`, `#123456`
- Support tickets: `T-260505-00117`, `T-YYMMDD-XXXXX`
- Helpdesk / Incident / Case IDs: `INC1234567`, `CAS-98765`, `SR-10293`
- Order / Tracking IDs: `ORD12345`, `ORD-98765`, `ORDER#54321`

## Rules
- Return ONLY raw JSON. No explanation, no markdown, no extra text.
- Extract ALL ticket/order IDs found in the query into the `ticket_ids` list. Return clean IDs (strip leading '#' symbols).
- If no ticket_id is found, set ticket_ids to empty list [].
- If intent is `marketing_promotional`, sentiment is typically `Neutral` and priority is `Low`.

## Output Format
{{
  "intent": "ticket_create" | "ticket_status" | "marketing_promotional" | "general_query",
  "ticket_ids": ["<id1>", "<id2>"] | [],
  "sentiment": "Angry" | "Neutral" | "Happy",
  "priority": "Critical" | "High" | "Medium" | "Low"
}}

## User Query
{query}
"""

    try:
        logger.info("🧠 Detecting intent using LLM")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "detect_intent_llm"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a JSON-only response system. "
                        "Return ONLY valid JSON. No markdown, no explanation, no extra text."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],  
            temperature=0,
            reasoning_effort="none"
        )

        output = res.choices[0].message.content.strip()
        logger.info(f"🧠 Intent raw output: {output}")

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in LLM response")

        cleaned_output = match.group(0).strip()
        logger.info(f"🧹 Cleaned JSON output: {cleaned_output}")

        data = json.loads(cleaned_output)
        intent = data.get("intent", "general_query")
        raw_ticket_ids = data.get("ticket_ids", [])
        sentiment = data.get("sentiment", "Neutral")
        priority = data.get("priority", "Medium")

        if isinstance(raw_ticket_ids, str):
            raw_ticket_ids = [raw_ticket_ids] if raw_ticket_ids else []

        cleaned_ticket_ids = []
        for tid in raw_ticket_ids:
            if isinstance(tid, str):
                c = tid.strip().lstrip("#").strip()
                if c and c not in cleaned_ticket_ids:
                    cleaned_ticket_ids.append(c)

        # Regex fallback verification if LLM missed ticket IDs
        if not cleaned_ticket_ids:
            regex_ids = extract_ticket_and_order_ids(query)
            if regex_ids:
                cleaned_ticket_ids = regex_ids
                logger.info(f"🔎 Regex supplemented ticket IDs: {cleaned_ticket_ids}")

        logger.info(f"✅ Intent detected: intent={intent}, ticket_ids={cleaned_ticket_ids}, sentiment={sentiment}, priority={priority}")
        return {
            "intent": intent, 
            "ticket_ids": cleaned_ticket_ids, 
            "sentiment": sentiment, 
            "priority": priority,
            "used_fallback": False
        }

    except Exception as e:
        logger.error(f"❌ Intent detection failed: {e}")

        ticket_ids = []
        try:
            ticket_ids = extract_ticket_and_order_ids(query)
        except Exception:
            pass

        fallback_intent = "ticket_status" if ticket_ids else "general_query"
        
        q_lower = query.lower()
        if any(w in q_lower for w in ["sue", "legal", "lawyer", "court", "scam"]):
            fallback_priority = "Critical"
            fallback_sentiment = "Angry"
        elif any(w in q_lower for w in ["refund", "cancel", "urgent", "wrong", "fake", "bad", "worst"]):
            fallback_priority = "High"
            fallback_sentiment = "Angry"
        elif any(w in q_lower for w in ["thanks", "thank you", "great", "good", "happy"]):
            fallback_priority = "Low"
            fallback_sentiment = "Happy"
        else:
            fallback_priority = "Medium"
            fallback_sentiment = "Neutral"

        logger.info(f"🔁 Fallback intent: intent={fallback_intent}, ticket_ids={ticket_ids}, sentiment={fallback_sentiment}, priority={fallback_priority}")
        return {
            "intent": fallback_intent, 
            "ticket_ids": ticket_ids, 
            "sentiment": fallback_sentiment, 
            "priority": fallback_priority,
            "used_fallback": True
        }



# ==============================
# ✉️ Generate Reply
# ==============================
def generate_reply_llm(
    context: str,
    query: str,
    agent_type: AgentType,
    from_email: str = None,
    is_ticket: bool = False,
    ticket_id: str = None,
    history: list = None
) -> str:
    """
    Generate professional email reply.
    history: list of prior conversation dicts from chat_history module.
    """

    response_tone = "Formal"
    agent_type_override = agent_type  # keep caller's value as fallback
    department_name = None
    company_name = None
    client_id = current_client_id.get()
    if client_id and client_id != "SYSTEM":
        try:
            from app.email_credential import get_email_account
            account = get_email_account(client_id)
            if account:
                response_tone   = account.get("response_tone", "Formal")
                agent_type_override = account.get("agent_type", agent_type)
                department_name = account.get("department_name")
                company_name    = account.get("company_name")
        except Exception as e:
            logger.warning(f"Failed to fetch account profile: {e}")

    tone_instruction = TONE_INSTRUCTIONS.get(
        response_tone,
        f"Write your reply in a {response_tone} tone."
    )
    base_persona = AGENT_PROMPTS.get(agent_type_override, AGENT_PROMPTS["customer_support"])
    system_prompt = f"{base_persona}\n\nCRITICAL BRAND VOICE GUIDELINE: {tone_instruction}"

    customer_name = (
        extract_name_from_email(from_email)
        if from_email
        else "Customer"
    )

    logger.info(f"👤 Customer name: {customer_name}")

    history_block = _format_history(history or [])
    if history_block:
        logger.info(f"📜 Injecting {len(history or [])} history messages into prompt")

    # ==========================================
    # 🎫 Ticket Reply
    # ==========================================
    if is_ticket and ticket_id:

        if department_name:
            team_name = department_name
        else:
            agent_team_map = {
                "customer_support":     "Customer Support Team",
                "ecommerce_support":    "E-Commerce Support Team",
                "technical_support":    "Technical Support Team",
                "billing_support":      "Billing & Invoicing Team",
                "executive_escalation": "Executive Support Team",
                "customer_support_agent":  "Customer Support Team",
                "ecommerce_support_agent": "E-Commerce Support Team",
                "crm_support_agent":       "CRM Support Team"
            }
            team_name = agent_team_map.get(agent_type_override, "Support Team")

        prompt = f"""Write a professional email reply to {customer_name} acknowledging that support ticket #{ticket_id} has been logged and is currently in progress.

Customer Query:
{query}

Ticket Information:
- Ticket ID: {ticket_id}
- Customer Name: {customer_name}
{context}

Requirements:
1. Start with greeting: Hi {customer_name},
2. Acknowledge that ticket #{ticket_id} is registered and the team is reviewing their inquiry.
3. Keep it to 2-3 sentences.
4. Conclude with:
Thanks & Regards,
{team_name}

Write ONLY the final email message text. No preamble, no quotes, no explanation, no bulleted instructions.
"""

    # ==========================================
    # 💬 General Reply
    # ==========================================
    else:

        prompt = f"""
Customer Name:
{customer_name}

{history_block}

Context:
{context}

Customer Query:
{query}

Instructions:
- Be professional
- Be concise
- Do NOT hallucinate
- NEVER claim or state that a ticket has been created, and NEVER output placeholder ticket references like "[Insert Ticket ID]" or "[Ticket Number]" or "[Ticket ID]".
- If previous conversation exists above, maintain continuity — do not repeat what was already addressed
- If no answer available, say politely
- End professionally

Write the email reply.

Email Ending
Thanks & Regards,
dont add name section example "[Your Name]"
Department: {department_name or 'derive from agent_type'}
Company: {company_name or 'derive from context/email'}
"""

    try:
        logger.info("📧 Generating AI reply")
        logger.info(f"📋📋📋 Prompt context being sent to LLM: {context[:1000] if context else 'EMPTY'}")
        logger.info(f"📋📋📋 Ending")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_reply_llm"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            reasoning_effort="none",
            max_tokens=1500
        )

        reply = strip_reasoning_and_think_tags(res.choices[0].message.content)
        
        # 1. Clean any trailing chain-of-thought analysis or numbered notes after the sign-off block
        signoff_match = re.search(r'((?:Thanks\s*(?:&|and)\s*Regards|Best\s*regards|Sincerely)[\s\S]*?\n[^\n]+)', reply, flags=re.IGNORECASE)
        if signoff_match:
            reply = reply[:signoff_match.end()].strip()

        # 2. Filter out any echoed prompt/instruction bullet lines
        clean_lines = []
        for line in reply.split("\n"):
            clean_l = line.strip().strip('"').strip("'")
            if re.match(r'^\*?\s*(?:Write\s+\d|Mention\s+team|End\s+with|Return\s+ONLY|Start\s+with|Requirements:)', clean_l, flags=re.IGNORECASE):
                continue
            clean_lines.append(line)
        reply = "\n".join(clean_lines).strip().strip('"').strip("'")

        logger.info(f"✅ Reply generated successfully: {reply[:150]}...")
        return reply

    except Exception as e:
        logger.error(f"❌ Reply generation failed: {e}")
        return (
            "Sorry, we are unable to process "
            "your request at the moment."
        )


def design_payload(
    paylod1: dict,
    mail_id: str,
    subject: str,
    body: str,
    status: str,
    personal_details: dict = None
) -> dict:

    personal_details = personal_details or {}

    prompt = f"""
You are a payload mapping system.

You will be given a TEMPLATE payload and dynamic input data.
Your job is to map the dynamic input data into the same structure as the TEMPLATE payload.

TEMPLATE PAYLOAD:
{json.dumps(paylod1, indent=2)}

DYNAMIC INPUT DATA:
- mail_id: {mail_id}
- subject: {subject}
- body: {body}
- status: {status}

BODY CLEANING RULES:
- Remove email signatures (lines starting with "--")
- Remove disclaimer sections ("DISCLAIMER:", "This email and its attachments")
- Remove forwarded email headers ("From:", "Sent:", "To:", "Cc:")
- Use only the core message content for "description" or similar fields

PERSONAL DETAILS:
{json.dumps(personal_details, indent=2)}

MAPPING RULES:
- Keep all keys from the TEMPLATE exactly as they are
- Keep all values from the TEMPLATE that are NOT related to the dynamic input
- Replace ONLY the values that logically match the dynamic input:
  * "description" or similar → use body
  * "email" → use mail_id
  * "ticket_status" → use status
  * "subject" or similar → use subject
- Map PERSONAL DETAILS into the template where logical:
  * "person_name", "name", "customer_name" or similar → use personal_details name fields
  * "first_name" → use personal_details first_name if available
  * "last_name" → use personal_details last_name if available
  * "mobile_no", "phone", "contact" or similar → use personal_details phone/mobile if available
  * If a personal detail field has no match in template, ignore it
  * If template has a personal field but personal_details is empty, keep template value as-is
- Do NOT add new keys
- Do NOT remove existing keys
- Do NOT change data types

Return ONLY valid JSON. No explanation. No markdown. No extra text.
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "design_payload"),
            messages=[
                {
                    "role": "system",
                    "content": "You are a JSON-only response system. Return ONLY valid JSON. No markdown. No explanation."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        output = res.choices[0].message.content.strip()

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON found in LLM response")

        return json.loads(match.group(0))

    except Exception as e:
        logger.error(f"❌ Payload design failed: {e}")
        fallback = paylod1.copy()
        fallback["description"] = body
        fallback["email"] = mail_id
        fallback["ticket_status"] = status
        if personal_details:
            fallback["person_name"] = personal_details.get("name") or personal_details.get("person_name", fallback.get("person_name", ""))
            fallback["mobile_no"] = personal_details.get("mobile") or personal_details.get("mobile_no", fallback.get("mobile_no", ""))
        return fallback


# ==============================
# 🔍 Scan History for Ticket ID
# ==============================
def scan_history_for_ticket(query: str, history: list) -> dict:
    """
    LLM scans conversation history to find a relevant ticket/order ID
    for the current customer query.

    Returns:
        {"found": True,  "ticket_id": "275424000000399001", "ambiguous": False}
        {"found": False, "ticket_id": None, "ambiguous": False}
        {"found": True, "ticket_id": None, "ambiguous": True, "ticket_ids": [...]}
    """
    if not history:
        return {"found": False, "ticket_id": None, "ambiguous": False}

    # Format history for prompt
    history_text = _format_history(history)

    prompt = f"""
You are a support assistant analyzing a conversation history to find a relevant ticket or order ID.

## Current Customer Query
{query}

## Conversation History
{history_text}

## Task
1. Look through the conversation history for any ticket/case IDs (e.g. #275424000000399001, T-260601-12345, INC123456) or order IDs (e.g. ORD12345, #98765).
2. Determine if any of them are relevant to the current query.
3. If one is clearly relevant, return it (clean ID without '#').
4. If multiple exist and you cannot determine which is relevant, return all of them as ambiguous.
5. If none are relevant or none exist, return not found.

## Output Format
Return ONLY valid JSON. No explanation. No markdown.

If one relevant ID found:
{{"found": true, "ticket_id": "<id>", "ambiguous": false}}

If multiple found and cannot decide:
{{"found": true, "ticket_id": null, "ambiguous": true, "ticket_ids": ["<id1>", "<id2>"]}}

If none found:
{{"found": false, "ticket_id": null, "ambiguous": false}}
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "scan_history_for_ticket"),
            messages=[
                {
                    "role": "system",
                    "content": "You are a JSON-only response system. Return ONLY valid JSON. No markdown. No explanation."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        output = res.choices[0].message.content.strip()
        logger.info(f"🔍 History scan raw output: {output}")

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON found in history scan response")

        data = json.loads(match.group(0).strip())
        
        # Clean ticket_id or ticket_ids in output
        tid = data.get("ticket_id")
        if isinstance(tid, str):
            data["ticket_id"] = tid.strip().lstrip("#").strip()

        tids = data.get("ticket_ids", [])
        if isinstance(tids, list):
            cleaned_list = []
            for item in tids:
                if isinstance(item, str):
                    c = item.strip().lstrip("#").strip()
                    if c and c not in cleaned_list:
                        cleaned_list.append(c)
            data["ticket_ids"] = cleaned_list

        logger.info(f"✅ History scan result: {data}")
        return data

    except Exception as e:
        logger.error(f"❌ History scan failed: {e}")
        # Regex fallback scanning history
        history_ids = extract_ticket_and_order_ids(history_text)
        if len(history_ids) == 1:
            return {"found": True, "ticket_id": history_ids[0], "ambiguous": False}
        elif len(history_ids) > 1:
            return {"found": True, "ticket_id": None, "ambiguous": True, "ticket_ids": history_ids}
        return {"found": False, "ticket_id": None, "ambiguous": False}



# ==============================
# 🧹 Extract Issue Description
# ==============================
def extract_issue_description(body: str, history: list = None) -> str:
    """
    Extract a clean 2–3 sentence problem description from a customer email body.
    Strips greetings, signatures, prior-thread noise, and filler.
    Used before creating a ticket when the customer has described their issue
    after a failed ticket-ID verification.

    Returns a plain string suitable for use as ticket `problem_description`.
    Falls back to a truncated version of body on failure.
    """
    history_block = _format_history(history or [])

    prompt = f"""
You are a support ticket assistant. Extract a clean, concise problem description
from the customer email below.

## Rules
- Return 2–3 sentences maximum.
- Use only information present in the email body.
- Strip greetings, sign-offs, pleasantries, and email-thread boilerplate.
- Strip any prior quoted/forwarded content.
- Do NOT invent or infer details not stated by the customer.
- Return ONLY the plain description text. No labels, no JSON, no markdown.

{history_block}

## Customer Email Body
{body}
"""

    try:
        logger.info("🧹 Extracting issue description from customer body")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "extract_issue_description"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise extraction system. "
                        "Return only the plain extracted text, nothing else."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        description = res.choices[0].message.content.strip()
        logger.info(f"✅ Issue description extracted: {description[:120]}...")
        return description

    except Exception as e:
        logger.error(f"❌ Issue description extraction failed: {e}")
        # Graceful fallback: trim raw body to 500 chars
        return body[:500].strip()


# ==============================
# 📝 Generate Issue Summary
# ==============================
def generate_summary_llm(
    context: str,
    customer_body: str,
    history: list = None,
    old_summary: str = ""
) -> str:
    """
    Generate or update a concise 250-character summary of the customer issue.

    Sources used (in order of priority):
    1. old_summary — existing summary from MySQL chat_history (if any)
    2. history     — Redis conversation history for this customer+ticket
    3. context     — ticket/API context passed to generate_reply_llm
    4. customer_body — the current incoming email body

    Returns a plain string, max 250 characters.
    Falls back to a truncated customer_body on failure.
    """
    history_block = _format_history(history or [])

    old_summary_block = ""
    if old_summary:
        old_summary_block = f"## Existing Summary (update this, do not repeat it verbatim)\n{old_summary}\n"

    prompt = f"""
You are a support ticket summariser.
Generate a concise summary of the customer's issue in 250 characters or less.

## Rules
- Maximum 250 characters — hard limit, no exceptions.
- Plain text only. No bullet points, no labels, no JSON, no markdown.
- Capture: what the problem is, current status if known, any resolution steps taken.
- If an existing summary is provided, update it with new information — do not repeat it verbatim.
- Do NOT include customer name, ticket ID, or email address.
- Do NOT invent details not present in the sources below.

{old_summary_block}

## Conversation History
{history_block if history_block else "No prior history."}

## Ticket / API Context
{context if context else "No context available."}

## Current Customer Email
{customer_body}

Return ONLY the plain summary text. Nothing else.
"""

    try:
        logger.info("📝 Generating issue summary")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_summary_llm"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise summariser. "
                        "Return only plain text under 250 characters. No labels, no formatting."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        summary = res.choices[0].message.content.strip()

        # Hard enforce 250 char limit
        if len(summary) > 250:
            summary = summary[:247] + "..."

        logger.info(f"✅ Summary generated: {summary}")
        return summary

    except Exception as e:
        logger.error(f"❌ Summary generation failed: {e}")
        return customer_body[:247].strip() + "..." if len(customer_body) > 247 else customer_body.strip()


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