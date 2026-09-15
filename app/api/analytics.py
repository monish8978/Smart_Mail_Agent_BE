import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.auth_deps import get_current_user, require_client_access
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Analytics & Dashboard"])


def ensure_llm_logs_table():
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50) NOT NULL,
                    provider VARCHAR(50) NOT NULL DEFAULT 'groq',
                    model_name VARCHAR(100) NOT NULL,
                    prompt_tokens INT NOT NULL,
                    completion_tokens INT NOT NULL,
                    cost DECIMAL(10, 6) NOT NULL,
                    billed_cost DECIMAL(10, 6) DEFAULT NULL,
                    latency_ms INT NOT NULL,
                    caller_function VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN provider VARCHAR(50) NOT NULL DEFAULT 'groq'")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN billed_cost DECIMAL(10, 6) DEFAULT NULL")
                except Exception:
                    pass
                db.commit()
        logger.info("✅ llm_logs table ensured")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure llm_logs table: {e}")


@router.get("/dashboard/stats/{client_id}")
@router.get("/dashboard-stats/{client_id}")
def get_dashboard_stats_endpoint(
    client_id: str,
    range_type: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    try:
        stats_data = {
            "total_emails": 0,
            "pending_emails": 0,
            "ai_replies": 0,
            "failed_emails": 0,
            "tickets_generated": 0,
            "orders_tracked": 0,
            "active_accounts": 0,
            "avg_confidence": 0.0,
            "chart_data": []
        }

        date_filter = ""
        date_params = []

        if range_type == "today":
            date_filter = " AND DATE(created_at) = CURDATE()"
        elif range_type == "yesterday":
            date_filter = " AND DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"
        elif range_type == "this_month":
            date_filter = " AND MONTH(created_at) = MONTH(CURDATE()) AND YEAR(created_at) = YEAR(CURDATE())"
        elif range_type == "last_month":
            date_filter = " AND MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))"
        elif range_type == "custom" and start_date and end_date:
            date_filter = " AND DATE(created_at) BETWEEN %s AND %s"
            date_params = [start_date, end_date]

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_logs: {ex}")

                acct_col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_accounts")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        acct_col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_accounts: {ex}")

                # 1. Total Emails
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE 1=1" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s" + date_filter, (client_id, *date_params))
                stats_data["total_emails"] = cursor.fetchone()[0]

                # 2. Pending Emails
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE status = 'pending'" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status = 'pending'" + date_filter, (client_id, *date_params))
                stats_data["pending_emails"] = cursor.fetchone()[0]

                # 3. AI Replies Sent
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE status IN ('sent', 'ticket_created_and_sent')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('sent', 'ticket_created_and_sent')" + date_filter, (client_id, *date_params))
                stats_data["ai_replies"] = cursor.fetchone()[0]

                # 4. Failed / Review-Required Emails
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE status IN ('send_failed', 'ticket_created_send_failed', 'pending_manual_review', 'ticket_creation_failed')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('send_failed', 'ticket_created_send_failed', 'pending_manual_review', 'ticket_creation_failed')" + date_filter, (client_id, *date_params))
                stats_data["failed_emails"] = cursor.fetchone()[0]

                # 5. Tickets Generated
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE status IN ('ticket_created_and_sent', 'ticket_created_send_failed')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('ticket_created_and_sent', 'ticket_created_send_failed')" + date_filter, (client_id, *date_params))
                stats_data["tickets_generated"] = cursor.fetchone()[0]

                # 6. Orders Tracked
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_logs WHERE (subject LIKE '%%order%%' OR body LIKE '%%order%%')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND (subject LIKE '%%order%%' OR body LIKE '%%order%%')" + date_filter, (client_id, *date_params))
                stats_data["orders_tracked"] = cursor.fetchone()[0]

                # 7. Active Accounts
                if client_id == "ALL":
                    cursor.execute("SELECT COUNT(*) FROM email_accounts")
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_accounts WHERE {acct_col} = %s", (client_id,))
                stats_data["active_accounts"] = cursor.fetchone()[0]

                # 7b. Average AI Confidence
                if client_id == "ALL":
                    cursor.execute("SELECT AVG(score) FROM email_logs WHERE score IS NOT NULL" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT AVG(score) FROM email_logs WHERE {col} = %s AND score IS NOT NULL" + date_filter, (client_id, *date_params))
                avg_score = cursor.fetchone()[0]
                stats_data["avg_confidence"] = round(float(avg_score), 1) if avg_score is not None else 0.0

                # 8. Dynamic Chart Data
                group_by_format = "%%Y-%%m-%%d"
                group_by_name = "%%b %%d"
                chart_interval = "1=1"

                if range_type == "today":
                    group_by_format = "%%H:00"
                    group_by_name = "%%h %%p"
                    chart_interval = "DATE(created_at) = CURDATE()"
                elif range_type == "yesterday":
                    group_by_format = "%%H:00"
                    group_by_name = "%%h %%p"
                    chart_interval = "DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"
                elif range_type == "this_month":
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "MONTH(created_at) = MONTH(CURDATE()) AND YEAR(created_at) = YEAR(CURDATE())"
                elif range_type == "last_month":
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))"
                elif range_type == "custom" and start_date and end_date:
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "DATE(created_at) BETWEEN %s AND %s"

                chart_params = []
                if client_id != "ALL":
                    chart_params.append(client_id)
                if range_type == "custom" and start_date and end_date:
                    chart_params.extend([start_date, end_date])

                where_clause = f"{chart_interval}" if client_id == "ALL" else f"{col} = %s AND {chart_interval}"

                cursor.execute(f"""
                    SELECT DATE_FORMAT(created_at, '{group_by_name}') as formatted_time, 
                           COUNT(*) as emails, 
                           SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as ai_replies,
                           DATE_FORMAT(created_at, '{group_by_format}') as sort_key
                    FROM email_logs
                    WHERE {where_clause}
                    GROUP BY sort_key, formatted_time
                    ORDER BY sort_key ASC
                """, tuple(chart_params))
                rows = cursor.fetchall()

        chart_data = []
        for row in rows:
            chart_data.append({
                "name": row[0],
                "emails": row[1],
                "aiReplied": int(row[2] or 0)
            })

        stats_data["chart_data"] = chart_data
        return stats_data

    except Exception as e:
        logger.error(f"❌ Failed to fetch dashboard stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/llm/metrics/{client_id}")
def get_llm_metrics_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        ensure_llm_logs_table()
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # 1. Total statistics
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            COUNT(id) as total_requests,
                            SUM(prompt_tokens) as total_prompt_tokens,
                            SUM(completion_tokens) as total_completion_tokens,
                            SUM(cost) as total_cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            COUNT(id) as total_requests,
                            SUM(prompt_tokens) as total_prompt_tokens,
                            SUM(completion_tokens) as total_completion_tokens,
                            SUM(cost) as total_cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                    """, (client_id,))
                total_row = cursor.fetchone()

                totals = {
                    "total_requests": int(total_row[0] or 0),
                    "total_prompt_tokens": int(total_row[1] or 0),
                    "total_completion_tokens": int(total_row[2] or 0),
                    "total_cost": float(total_row[3] or 0.0),
                    "avg_latency": float(total_row[4] or 0.0)
                }

                # 2. Breakdown by Provider
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            COALESCE(NULLIF(provider, ''), 'groq') as provider_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY COALESCE(NULLIF(provider, ''), 'groq')
                        ORDER BY cost DESC, requests DESC
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            COALESCE(NULLIF(provider, ''), 'groq') as provider_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY COALESCE(NULLIF(provider, ''), 'groq')
                        ORDER BY cost DESC, requests DESC
                    """, (client_id,))
                provider_rows = cursor.fetchall()
                provider_breakdown = []
                for row in provider_rows:
                    provider_breakdown.append({
                        "provider": str(row[0]).lower(),
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })

                # 3. Breakdown by Model
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            model_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY model_name
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            model_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY model_name
                    """, (client_id,))
                model_rows = cursor.fetchall()
                model_breakdown = []
                for row in model_rows:
                    model_breakdown.append({
                        "model_name": row[0],
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })

                # 4. Breakdown by Caller Function
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            caller_function,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY caller_function
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            caller_function,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY caller_function
                    """, (client_id,))
                caller_rows = cursor.fetchall()
                caller_breakdown = []
                for row in caller_rows:
                    func_name = row[0]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    else:
                        func_display = func_name.replace("_", " ").title()

                    caller_breakdown.append({
                        "caller_function": row[0],
                        "caller_display": func_display,
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })

                # 5. Recent Logs (last 30)
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            id, COALESCE(NULLIF(provider, ''), 'groq') as provider, model_name, prompt_tokens, completion_tokens, cost, latency_ms, caller_function, created_at
                        FROM llm_logs
                        ORDER BY created_at DESC
                        LIMIT 30
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            id, COALESCE(NULLIF(provider, ''), 'groq') as provider, model_name, prompt_tokens, completion_tokens, cost, latency_ms, caller_function, created_at
                        FROM llm_logs
                        WHERE client_id = %s
                        ORDER BY created_at DESC
                        LIMIT 30
                    """, (client_id,))
                log_rows = cursor.fetchall()
                recent_logs = []
                for row in log_rows:
                    func_name = row[7]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    else:
                        func_display = func_name.replace("_", " ").title()

                    recent_logs.append({
                        "id": row[0],
                        "provider": row[1],
                        "model_name": row[2],
                        "prompt_tokens": int(row[3]),
                        "completion_tokens": int(row[4]),
                        "cost": float(row[5]),
                        "latency_ms": int(row[6]),
                        "caller_function": row[7],
                        "caller_display": func_display,
                        "created_at": row[8].strftime("%b %d, %H:%M:%S") if row[8] else ""
                    })

                # 6. Budget Info
                from app.email_credential import get_budget_status
                budget_info = get_budget_status(client_id, cursor)

                return {
                    "status": "success",
                    "totals": totals,
                    "providers": provider_breakdown,
                    "models": model_breakdown,
                    "callers": caller_breakdown,
                    "logs": recent_logs,
                    "budget": budget_info
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch LLM analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
