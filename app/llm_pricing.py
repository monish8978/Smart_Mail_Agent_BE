import logging

logger = logging.getLogger(__name__)

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
