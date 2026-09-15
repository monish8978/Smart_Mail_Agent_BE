import os
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import pymysql

from app.auth_deps import get_current_user, require_admin, require_client_access
from app.db import get_db_ctx
from app.email_credential import get_budget_status
from .crypto import _encrypt_key, _decrypt_key

logger = logging.getLogger(__name__)

router = APIRouter()


# ==============================
# 🤖 Client LLM Configurations
# ==============================

class ClientLlmConfigRequest(BaseModel):
    client_id: str
    caller_function: str
    global_config_id: int | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_name: str
    api_version: str | None = None


@router.post("/admin/client-llm-config")
def set_client_llm_config(data: ClientLlmConfigRequest, user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            enc_api_key = _encrypt_key(data.api_key)
            cursor.execute("""
                INSERT INTO client_llm_config (client_id, caller_function, global_config_id, provider, api_key, base_url, model_name, api_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    global_config_id=%s, provider=%s, api_key=%s, base_url=%s, model_name=%s, api_version=%s
            """, (
                data.client_id, data.caller_function, data.global_config_id, data.provider, enc_api_key, data.base_url, data.model_name, data.api_version,
                data.global_config_id, data.provider, enc_api_key, data.base_url, data.model_name, data.api_version
            ))
            db.commit()
    return {"status": "success"}


@router.get("/admin/client-llm-config/{client_id}")
def get_client_llm_config(client_id: str, user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT caller_function, model_name, global_config_id, provider, api_key, base_url, api_version, created_at, updated_at, refreshed
                FROM client_llm_config 
                WHERE client_id=%s
            """, (client_id,))
            rows = cursor.fetchall()
    return [{
        "caller_function": r[0],
        "model_name": r[1],
        "global_config_id": r[2],
        "provider": r[3],
        "api_key": _decrypt_key(r[4]),
        "base_url": r[5],
        "api_version": r[6],
        "created_at": str(r[7]) if r[7] else None,
        "updated_at": str(r[8]) if r[8] else None,
        "refreshed": str(r[9]) if r[9] else None
    } for r in rows]


class ClientLlmRefreshRequest(BaseModel):
    client_id: str
    caller_function: str
    global_config_id: int | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api_version: str | None = None


@router.post("/admin/client-llm-config/refresh")
def refresh_client_llm_config(data: ClientLlmRefreshRequest, user: dict = Depends(require_admin())):
    target_provider = data.provider
    target_api_key = data.api_key
    target_base_url = data.base_url
    target_api_version = data.api_version
    global_config_id = data.global_config_id

    if global_config_id:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, api_version, name 
                    FROM globally_available_llm_configs 
                    WHERE id = %s
                """, (global_config_id,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Referenced globally available LLM config not found")
                target_provider = row[1]
                target_api_key = _decrypt_key(row[2])
                target_base_url = row[3]
                target_api_version = row[4]
    elif not target_provider and not target_api_key:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, api_version, model_name 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    cursor.execute("SELECT id, provider, api_key, base_url, api_version, model_name FROM global_default_llm LIMIT 1")
                    row = cursor.fetchone()
                if row:
                    target_provider = row[1]
                    target_api_key = _decrypt_key(row[2])
                    target_base_url = row[3]
                    target_api_version = row[4]

    if not target_provider or not target_api_key:
        raise HTTPException(status_code=400, detail="No global default or custom LLM provider credentials configured in the database to perform live refresh.")

    try:
        models = _query_provider_live_models(target_provider, target_api_key, target_base_url, target_api_version)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying live models from {target_provider}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch live models from {target_provider.upper()}: {str(e)}")

    now_str = None
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if global_config_id:
                    cursor.execute("""
                        UPDATE globally_available_llm_configs 
                        SET refreshed = CURRENT_TIMESTAMP 
                        WHERE id = %s
                    """, (global_config_id,))
                elif not data.provider:
                    cursor.execute("UPDATE global_default_llm SET refreshed = CURRENT_TIMESTAMP WHERE id = 1")

                cursor.execute("""
                    INSERT INTO client_llm_config (client_id, caller_function, model_name, global_config_id, provider, api_key, base_url, api_version, refreshed)
                    VALUES (%s, %s, '', %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON DUPLICATE KEY UPDATE 
                        global_config_id=VALUES(global_config_id),
                        provider=VALUES(provider),
                        api_key=VALUES(api_key),
                        base_url=VALUES(base_url),
                        api_version=VALUES(api_version),
                        refreshed=CURRENT_TIMESTAMP
                """, (
                    data.client_id, data.caller_function, global_config_id,
                    data.provider if not global_config_id else None,
                    _encrypt_key(data.api_key, client_id=data.client_id) if not global_config_id else None,
                    data.base_url if not global_config_id else None,
                    data.api_version if not global_config_id else None
                ))
                
                cursor.execute("""
                    SELECT refreshed FROM client_llm_config WHERE client_id=%s AND caller_function=%s
                """, (data.client_id, data.caller_function))
                r = cursor.fetchone()
                if r and r[0]:
                    now_str = str(r[0])
                db.commit()
    except Exception as e:
        logger.error(f"Database error during client LLM refresh: {e}")
        raise HTTPException(status_code=400, detail=f"Database update failed: {str(e)}")

    return {
        "status": "success",
        "client_id": data.client_id,
        "caller_function": data.caller_function,
        "global_config_id": global_config_id,
        "provider": target_provider,
        "refreshed": now_str,
        "count": len(models),
        "models": models
    }


# Backward compatibility aliases
@router.post("/admin/client-model-config")
def set_client_model_config_legacy(data: ClientLlmConfigRequest, user: dict = Depends(require_admin())):
    return set_client_llm_config(data, user)


@router.get("/admin/client-model-config/{client_id}")
def get_client_model_config_legacy(client_id: str, user: dict = Depends(require_admin())):
    return get_client_llm_config(client_id, user)


class ClientCostConfigRequest(BaseModel):
    client_id: str
    cost_multiplier: float
    monthly_budget_usd: float | None = None


@router.post("/admin/client-cost-config")
def set_client_cost_config(data: ClientCostConfigRequest, user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE email_accounts SET cost_multiplier=%s, monthly_budget_usd=%s WHERE client_id=%s",
                (data.cost_multiplier, data.monthly_budget_usd, data.client_id)
            )
            db.commit()
    return {"status": "success"}


# ==============================
# 🌐 Global Default LLM
# ==============================

class GlobalDefaultLlmRequest(BaseModel):
    provider: str
    api_key: str
    base_url: str | None = None
    model_name: str
    api_version: str | None = None
    is_override_active: bool | None = None


class ToggleOverrideRequest(BaseModel):
    is_override_active: bool


@router.get("/admin/global-default-llm")
def get_global_default_llm_endpoint(user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed, is_override_active 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    cursor.execute("SELECT id, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed, is_override_active FROM global_default_llm LIMIT 1")
                    row = cursor.fetchone()
                if not row:
                    return {
                        "id": 1,
                        "provider": "groq",
                        "api_key": os.getenv("GROQ_API_KEY", ""),
                        "base_url": "https://api.groq.com/openai/v1",
                        "model_name": os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b"),
                        "api_version": None,
                        "created_at": None,
                        "updated_at": None,
                        "refreshed": None,
                        "is_override_active": False
                    }
                return {
                    "id": row[0],
                    "provider": row[1],
                    "api_key": _decrypt_key(row[2]),
                    "base_url": row[3],
                    "model_name": row[4],
                    "api_version": row[5],
                    "created_at": str(row[6]) if row[6] else None,
                    "updated_at": str(row[7]) if row[7] else None,
                    "refreshed": str(row[8]) if row[8] else None,
                    "is_override_active": bool(row[9]) if len(row) > 9 and row[9] is not None else False
                }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/global-default-llm")
def set_global_default_llm_endpoint(data: GlobalDefaultLlmRequest, user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                enc_api_key = _encrypt_key(data.api_key)
                cursor.execute("""
                    INSERT INTO global_default_llm (id, provider, api_key, base_url, model_name, api_version)
                    VALUES (1, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                        provider=VALUES(provider),
                        api_key=VALUES(api_key),
                        base_url=VALUES(base_url),
                        model_name=VALUES(model_name),
                        api_version=VALUES(api_version)
                """, (data.provider, enc_api_key, data.base_url, data.model_name, data.api_version))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/global-default-llm/toggle-override")
def toggle_global_override_endpoint(data: ToggleOverrideRequest, user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    UPDATE global_default_llm 
                    SET is_override_active = %s 
                    WHERE id = 1
                """, (1 if data.is_override_active else 0,))
            db.commit()
        logger.info(f"⚙️ Global Default LLM Emergency Override toggled to: {data.is_override_active}")
        return {"status": "success", "is_override_active": data.is_override_active}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/global-default-llm/refresh")
def refresh_global_default_llm_endpoint(user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT provider, api_key, base_url, api_version 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Global Default LLM configuration not found")
                
                provider, api_key, base_url, api_version = row[0], _decrypt_key(row[1]), row[2], row[3]
                models = _query_provider_live_models(provider, api_key, base_url, api_version)
                
                cursor.execute("UPDATE global_default_llm SET refreshed = CURRENT_TIMESTAMP WHERE id = 1")
                db.commit()

                cursor.execute("SELECT refreshed FROM global_default_llm WHERE id = 1")
                refreshed_val = cursor.fetchone()[0]

                return {
                    "status": "success",
                    "provider": provider,
                    "refreshed": str(refreshed_val),
                    "count": len(models),
                    "models": models
                }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to refresh global default LLM models: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==============================
# 🧩 Globally Available LLM Configs
# ==============================

class GloballyAvailableLlmConfigRequest(BaseModel):
    id: int | None = None
    name: str
    provider: str
    api_key: str
    base_url: str | None = None
    model_name: str
    api_version: str | None = None


@router.get("/admin/globally-available-llm-configs")
def get_globally_available_llm_configs_endpoint(user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, name, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed 
                    FROM globally_available_llm_configs 
                    ORDER BY id DESC
                """)
                rows = cursor.fetchall()
                configs = []
                for r in rows:
                    configs.append({
                        "id": r[0],
                        "name": r[1],
                        "provider": r[2],
                        "api_key": _decrypt_key(r[3]),
                        "base_url": r[4],
                        "model_name": r[5],
                        "api_version": r[6],
                        "created_at": str(r[7]) if r[7] else None,
                        "updated_at": str(r[8]) if r[8] else None,
                        "refreshed": str(r[9]) if r[9] else None
                    })
                return configs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/globally-available-llm-configs")
def save_globally_available_llm_config_endpoint(data: GloballyAvailableLlmConfigRequest, user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                enc_api_key = _encrypt_key(data.api_key)
                if data.id:
                    cursor.execute("""
                        UPDATE globally_available_llm_configs 
                        SET name=%s, provider=%s, api_key=%s, base_url=%s, model_name=%s, api_version=%s
                        WHERE id=%s
                    """, (data.name, data.provider, enc_api_key, data.base_url, data.model_name, data.api_version, data.id))
                else:
                    cursor.execute("""
                        INSERT INTO globally_available_llm_configs (name, provider, api_key, base_url, model_name, api_version)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (data.name, data.provider, enc_api_key, data.base_url, data.model_name, data.api_version))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/admin/globally-available-llm-configs/{config_id}")
def delete_globally_available_llm_config_endpoint(config_id: int, user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("DELETE FROM globally_available_llm_configs WHERE id=%s", (config_id,))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/globally-available-llm-configs/{config_id}/refresh")
def refresh_globally_available_llm_config_endpoint(config_id: int, user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT provider, api_key, base_url, api_version, name 
                    FROM globally_available_llm_configs 
                    WHERE id=%s
                """, (config_id,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Globally available LLM configuration not found")
                
                provider, api_key, base_url, api_version, name = row[0], _decrypt_key(row[1]), row[2], row[3], row[4]
                models = _query_provider_live_models(provider, api_key, base_url, api_version)
                
                cursor.execute("""
                    UPDATE globally_available_llm_configs 
                    SET refreshed = CURRENT_TIMESTAMP 
                    WHERE id = %s
                """, (config_id,))
                db.commit()

                cursor.execute("SELECT refreshed FROM globally_available_llm_configs WHERE id=%s", (config_id,))
                refreshed_val = cursor.fetchone()[0]

                return {
                    "status": "success",
                    "config_id": config_id,
                    "name": name,
                    "provider": provider,
                    "refreshed": str(refreshed_val),
                    "count": len(models),
                    "models": models
                }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to refresh models for globally available config {config_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# Backward compatibility aliases for legacy /admin/llm-configs
@router.get("/admin/llm-configs")
def get_llm_configs_endpoint_legacy(user: dict = Depends(require_admin())):
    return get_globally_available_llm_configs_endpoint(user)


@router.post("/admin/llm-configs")
def save_llm_config_endpoint_legacy(data: GloballyAvailableLlmConfigRequest, user: dict = Depends(require_admin())):
    return save_globally_available_llm_config_endpoint(data, user)


@router.delete("/admin/llm-configs/{config_id}")
def delete_llm_config_endpoint_legacy(config_id: int, user: dict = Depends(require_admin())):
    return delete_globally_available_llm_config_endpoint(config_id, user)


@router.post("/admin/llm-configs/{config_id}/refresh")
def refresh_llm_config_endpoint_legacy(config_id: int, user: dict = Depends(require_admin())):
    return refresh_globally_available_llm_config_endpoint(config_id, user)


def _query_provider_live_models(provider: str, api_key: str, base_url: str | None = None, api_version: str | None = None) -> list[str]:
    import requests
    provider = (provider or "groq").lower().strip()
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required to query models from provider.")

    models = []
    if provider == "groq":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.groq.com/openai/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"Groq error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id") and m.get("active", True)]

    elif provider == "openai":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.openai.com/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"OpenAI error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        excluded = ("whisper", "dall-e", "tts", "embedding", "moderation", "davinci", "babbage", "curie", "text-search")
        models = [
            m.get("id") for m in raw_list 
            if m.get("id") and not any(ex in m.get("id").lower() for ex in excluded)
        ]

    elif provider in ("claude", "anthropic"):
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.anthropic.com/v1'}/models"
        resp = requests.get(target_url, headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }, timeout=12)
        if resp.ok:
            raw_list = resp.json().get("data", [])
            models = [m.get("id") for m in raw_list if m.get("id")]
        else:
            models = ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]

    elif provider == "gemini":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://generativelanguage.googleapis.com/v1beta/openai'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if resp.ok:
            raw_list = resp.json().get("data", [])
            models = [m.get("id") for m in raw_list if m.get("id")]
        else:
            native_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            native_resp = requests.get(native_url, timeout=12)
            if native_resp.ok:
                raw_models = native_resp.json().get("models", [])
                models = [
                    m.get("name", "").replace("models/", "")
                    for m in raw_models
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
            else:
                raise Exception(f"Google Gemini error ({native_resp.status_code}): {native_resp.text}")

    elif provider == "grok":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.x.ai/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"xAI Grok error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    elif provider == "azure":
        target_url = f"{base_url.rstrip('/')}/openai/models?api-version={api_version or '2024-02-15-preview'}"
        resp = requests.get(target_url, headers={"api-key": api_key}, timeout=12)
        if not resp.ok:
            raise Exception(f"Azure OpenAI error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    else: # custom gateway
        target_url = f"{base_url.rstrip('/')}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=12)
        if not resp.ok:
            raise Exception(f"Custom gateway error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    return sorted(list(set(models)))


class FetchProviderModelsRequest(BaseModel):
    provider: str
    api_key: str
    base_url: str | None = None
    api_version: str | None = None


@router.post("/admin/llm/fetch-models")
def fetch_provider_models_endpoint(data: FetchProviderModelsRequest, user: dict = Depends(require_admin())):
    try:
        clean_models = _query_provider_live_models(data.provider, data.api_key, data.base_url, data.api_version)
        return {
            "status": "success", 
            "provider": (data.provider or "groq").lower().strip(), 
            "count": len(clean_models), 
            "models": clean_models
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch live models for {data.provider}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==============================
# 💰 Budget & Costs
# ==============================

@router.get("/budget-status/{client_id}")
def get_budget_status_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            return get_budget_status(client_id, cursor)


@router.get("/admin/budget-status")
def get_all_budget_statuses(user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id FROM email_accounts")
            client_ids = [r[0] for r in cursor.fetchall()]
            results = []
            for cid in client_ids:
                status = get_budget_status(cid, cursor)
                status["client_id"] = cid
                results.append(status)
    return results
