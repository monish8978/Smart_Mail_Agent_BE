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
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN email_log_id INT DEFAULT NULL")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN thread_id VARCHAR(100) DEFAULT NULL")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD INDEX idx_llm_client_created (client_id, created_at)")
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
            "threshold": 80,
            "confidence_breakdown": {
                "passed_threshold": 0,
                "below_threshold": 0,
                "hallucination_risk": 0,
                "high": 0,
                "borderline": 0,
                "low": 0
            },
            "escalation_risk_count": 0,
            "oldest_pending_seconds": 0,
            "worker_heartbeat": {"status": "offline", "last_seen_sec": None, "label": "Offline"},
            "client_sync": None,
            "client_breakdown": [],
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

                # 7c. Policy-Aligned Confidence & Risk Floor Distribution
                active_threshold = 80
                try:
                    if client_id != "ALL":
                        cursor.execute(f"SELECT score_threshold FROM email_accounts WHERE {acct_col} = %s LIMIT 1", (client_id,))
                        t_row = cursor.fetchone()
                        if t_row and t_row[0] is not None:
                            active_threshold = int(t_row[0])
                    else:
                        cursor.execute("SELECT AVG(score_threshold) FROM email_accounts WHERE score_threshold IS NOT NULL")
                        avg_t_row = cursor.fetchone()
                        if avg_t_row and avg_t_row[0] is not None:
                            active_threshold = int(round(float(avg_t_row[0])))
                except Exception as t_err:
                    logger.warning(f"⚠️ Could not load score_threshold: {t_err}")

                stats_data["threshold"] = active_threshold

                if client_id == "ALL":
                    cursor.execute(f"""
                        SELECT 
                            SUM(CASE WHEN e.score >= COALESCE(a.score_threshold, 80) THEN 1 ELSE 0 END) as passed_thresh,
                            SUM(CASE WHEN e.score < COALESCE(a.score_threshold, 80) AND e.score IS NOT NULL THEN 1 ELSE 0 END) as below_thresh,
                            SUM(CASE WHEN e.score < 60 AND e.score IS NOT NULL THEN 1 ELSE 0 END) as low_floor
                        FROM email_logs e
                        LEFT JOIN email_accounts a ON e.{col} = a.{acct_col}
                        WHERE 1=1 {date_filter}
                    """, tuple(date_params))
                else:
                    cursor.execute(f"""
                        SELECT 
                            SUM(CASE WHEN score >= %s THEN 1 ELSE 0 END) as passed_thresh,
                            SUM(CASE WHEN score < %s AND score IS NOT NULL THEN 1 ELSE 0 END) as below_thresh,
                            SUM(CASE WHEN score < 60 AND score IS NOT NULL THEN 1 ELSE 0 END) as low_floor
                        FROM email_logs 
                        WHERE {col} = %s {date_filter}
                    """, (active_threshold, active_threshold, client_id, *date_params))

                conf_row = cursor.fetchone()
                if conf_row:
                    passed = int(conf_row[0] or 0)
                    below = int(conf_row[1] or 0)
                    floor_risk = int(conf_row[2] or 0)
                    stats_data["confidence_breakdown"] = {
                        "passed_threshold": passed,
                        "below_threshold": below,
                        "hallucination_risk": floor_risk,
                        # Aliases for backward compatibility
                        "high": passed,
                        "borderline": below,
                        "low": floor_risk
                    }

                # 7d. Escalation / Negative Sentiment Risk Count
                risk_filter = "WHERE (sentiment IN ('Frustrated', 'Urgent', 'Negative') OR priority IN ('High', 'Critical'))" + date_filter
                if client_id != "ALL":
                    risk_filter = f"WHERE {col} = %s AND (sentiment IN ('Frustrated', 'Urgent', 'Negative') OR priority IN ('High', 'Critical'))" + date_filter
                cursor.execute(f"SELECT COUNT(*) FROM email_logs {risk_filter}", (client_id, *date_params) if client_id != "ALL" else tuple(date_params))
                stats_data["escalation_risk_count"] = cursor.fetchone()[0]

                # 7e. Queue SLA - Age of oldest pending email in seconds
                queue_filter = "WHERE status = 'pending'" if client_id == "ALL" else f"WHERE {col} = %s AND status = 'pending'"
                cursor.execute(f"SELECT TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()) FROM email_logs {queue_filter}", (client_id,) if client_id != "ALL" else ())
                q_row = cursor.fetchone()
                stats_data["oldest_pending_seconds"] = int(q_row[0]) if (q_row and q_row[0] is not None) else 0

                # 7f. Multi-tenant Client Performance Breakdown (for ALL mode)
                if client_id == "ALL":
                    try:
                        cursor.execute(f"""
                            SELECT 
                                COALESCE(e.{col}, 'UNKNOWN') as cid,
                                COALESCE(MAX(a.company_name), COALESCE(e.{col}, 'UNKNOWN')) as company_name,
                                COUNT(e.id) as total_emails,
                                SUM(CASE WHEN e.status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as ai_replies,
                                SUM(CASE WHEN e.status = 'pending' THEN 1 ELSE 0 END) as pending_emails,
                                SUM(CASE WHEN e.status IN ('send_failed', 'ticket_created_send_failed', 'pending_manual_review', 'ticket_creation_failed') THEN 1 ELSE 0 END) as failed_emails,
                                ROUND(AVG(e.score), 1) as avg_score
                            FROM email_logs e
                            LEFT JOIN email_accounts a ON e.{col} = a.{acct_col}
                            WHERE 1=1 {date_filter}
                            GROUP BY cid
                            ORDER BY total_emails DESC
                            LIMIT 10
                        """, tuple(date_params))
                        c_breakdown = []
                        for crow in cursor.fetchall():
                            tot = crow[2] or 0
                            rep = int(crow[3] or 0)
                            rate = round((rep / tot) * 100) if tot > 0 else 0
                            c_breakdown.append({
                                "client_id": crow[0],
                                "company_name": crow[1],
                                "total_emails": tot,
                                "ai_replies": rep,
                                "pending_emails": int(crow[4] or 0),
                                "failed_emails": int(crow[5] or 0),
                                "avg_score": float(crow[6] or 0.0),
                                "automation_rate": rate
                            })
                        stats_data["client_breakdown"] = c_breakdown
                    except Exception as c_err:
                        logger.warning(f"⚠️ Failed to calculate client_breakdown: {c_err}")
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

        # 9. Real Worker Heartbeat & Client Ingestion Status from Redis
        try:
            import time
            from app.redis_pool import get_redis_main
            r = get_redis_main()
            now_ts = int(time.time())
            hb_val = r.get("imap_worker:heartbeat")
            if hb_val:
                hb_diff = max(0, now_ts - int(hb_val))
                if hb_diff <= 45:
                    stats_data["worker_heartbeat"] = {"status": "healthy", "last_seen_sec": hb_diff, "label": "Active"}
                elif hb_diff <= 150:
                    stats_data["worker_heartbeat"] = {"status": "delayed", "last_seen_sec": hb_diff, "label": "Delayed"}
                else:
                    stats_data["worker_heartbeat"] = {"status": "stalled", "last_seen_sec": hb_diff, "label": "Stalled"}
            else:
                # If Redis has no heartbeat record yet, fallback to active accounts check
                if stats_data["active_accounts"] > 0:
                    stats_data["worker_heartbeat"] = {"status": "standby", "last_seen_sec": None, "label": "Standby"}
                else:
                    stats_data["worker_heartbeat"] = {"status": "offline", "last_seen_sec": None, "label": "No Accounts"}

            if client_id != "ALL":
                sync_val = r.get(f"imap_sync:{client_id}")
                st_val = r.get(f"imap_status:{client_id}")
                stats_data["client_sync"] = {
                    "last_sync_sec": max(0, now_ts - int(sync_val)) if sync_val else None,
                    "status": st_val or ("connected" if stats_data["active_accounts"] > 0 else "unconfigured")
                }
            else:
                stats_data["client_sync"] = None
        except Exception as r_err:
            logger.warning(f"⚠️ Redis worker heartbeat query failed: {r_err}")
            stats_data["worker_heartbeat"] = {"status": "unknown", "last_seen_sec": None, "label": "Unknown"}
            stats_data["client_sync"] = None

        return stats_data

    except Exception as e:
        logger.error(f"❌ Failed to fetch dashboard stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/llm/metrics/{client_id}")
@router.get("/llm-metrics/{client_id}")
def get_llm_metrics_endpoint(client_id: str, time_window: str = "all", user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        ensure_llm_logs_table()

        # Build dynamic time window filter
        time_filter = ""
        if time_window == "24h":
            time_filter = " AND l.created_at >= NOW() - INTERVAL 24 HOUR"
        elif time_window == "7d":
            time_filter = " AND l.created_at >= NOW() - INTERVAL 7 DAY"
        elif time_window == "30d":
            time_filter = " AND l.created_at >= NOW() - INTERVAL 30 DAY"

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # 1. Total statistics
                if client_id == "ALL":
                    cursor.execute(f"""
                        SELECT 
                            COUNT(l.id) as total_requests,
                            SUM(l.prompt_tokens) as total_prompt_tokens,
                            SUM(l.completion_tokens) as total_completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as total_cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE 1=1 {time_filter}
                    """)
                else:
                    cursor.execute(f"""
                        SELECT 
                            COUNT(l.id) as total_requests,
                            SUM(l.prompt_tokens) as total_prompt_tokens,
                            SUM(l.completion_tokens) as total_completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as total_cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.client_id = %s {time_filter}
                    """, (client_id,))
                total_row = cursor.fetchone()

                totals = {
                    "total_requests": int(total_row[0] or 0),
                    "total_prompt_tokens": int(total_row[1] or 0),
                    "total_completion_tokens": int(total_row[2] or 0),
                    "total_cost": float(total_row[3] or 0.0),
                    "avg_latency": float(total_row[4] or 0.0),
                    "time_window": time_window
                }

                # 2. Breakdown by Provider
                if client_id == "ALL":
                    cursor.execute(f"""
                        SELECT 
                            COALESCE(NULLIF(l.provider, ''), 'groq') as provider_name,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE 1=1 {time_filter}
                        GROUP BY COALESCE(NULLIF(l.provider, ''), 'groq')
                        ORDER BY cost DESC, requests DESC
                    """)
                else:
                    cursor.execute(f"""
                        SELECT 
                            COALESCE(NULLIF(l.provider, ''), 'groq') as provider_name,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.client_id = %s {time_filter}
                        GROUP BY COALESCE(NULLIF(l.provider, ''), 'groq')
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
                    cursor.execute(f"""
                        SELECT 
                            l.model_name,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE 1=1 {time_filter}
                        GROUP BY l.model_name
                    """)
                else:
                    cursor.execute(f"""
                        SELECT 
                            l.model_name,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.client_id = %s {time_filter}
                        GROUP BY l.model_name
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
                    cursor.execute(f"""
                        SELECT 
                            l.caller_function,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE 1=1 {time_filter}
                        GROUP BY l.caller_function
                    """)
                else:
                    cursor.execute(f"""
                        SELECT 
                            l.caller_function,
                            COUNT(l.id) as requests,
                            SUM(l.prompt_tokens) as prompt_tokens,
                            SUM(l.completion_tokens) as completion_tokens,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost,
                            AVG(l.latency_ms) as avg_latency
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.client_id = %s {time_filter}
                        GROUP BY l.caller_function
                    """, (client_id,))
                caller_rows = cursor.fetchall()
                caller_breakdown = []
                for row in caller_rows:
                    func_name = row[0]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    elif func_name == "llm_score":
                        func_display = "Output Quality Evaluation"
                    elif func_name == "generate_summary_llm":
                        func_display = "Thread Summarization"
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

                # 5. Productive vs Guardrail / Evaluation Split
                productive_callers = {
                    "generate_reply_llm", "detect_intent_llm", "generate_issue_resolved_reply", 
                    "generate_off_topic_reply", "design_payload"
                }
                productive_cost = 0.0
                productive_reqs = 0
                guardrail_cost = 0.0
                guardrail_reqs = 0

                for c in caller_breakdown:
                    fn = c["caller_function"]
                    if fn in productive_callers or "reply" in fn or "intent" in fn:
                        productive_cost += c["cost"]
                        productive_reqs += c["requests"]
                    else:
                        guardrail_cost += c["cost"]
                        guardrail_reqs += c["requests"]

                total_split_cost = productive_cost + guardrail_cost
                guardrail_percent = round((guardrail_cost / total_split_cost * 100), 1) if total_split_cost > 0 else 0.0

                guardrail_analysis = {
                    "productive_cost": round(productive_cost, 6),
                    "productive_requests": productive_reqs,
                    "guardrail_cost": round(guardrail_cost, 6),
                    "guardrail_requests": guardrail_reqs,
                    "guardrail_tax_percent": guardrail_percent
                }

                # 6. 14-day Daily Trend
                trend_cond = "l.created_at >= NOW() - INTERVAL 14 DAY"
                trend_params = []
                if client_id != "ALL":
                    trend_cond += " AND l.client_id = %s"
                    trend_params.append(client_id)

                trend_sql = f"""
                    SELECT 
                        DATE(l.created_at) as log_date,
                        COUNT(l.id) as requests,
                        SUM(l.prompt_tokens + l.completion_tokens) as total_tokens,
                        SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as daily_cost
                    FROM llm_logs l
                    LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                    WHERE {trend_cond}
                    GROUP BY DATE(l.created_at)
                    ORDER BY DATE(l.created_at) ASC
                """
                if trend_params:
                    cursor.execute(trend_sql, tuple(trend_params))
                else:
                    cursor.execute(trend_sql)

                trend_rows = cursor.fetchall()
                daily_trends = [
                    {
                        "date": r[0].strftime("%b %d") if hasattr(r[0], "strftime") else str(r[0]),
                        "requests": int(r[1] or 0),
                        "tokens": int(r[2] or 0),
                        "cost": float(r[3] or 0.0)
                    }
                    for r in trend_rows
                ]

                # 7. Recent Logs (last 30) with thread & email attribution
                recent_logs_cond = f"WHERE 1=1 {time_filter}" if client_id == "ALL" else f"WHERE l.client_id = %s {time_filter}"
                recent_logs_params = () if client_id == "ALL" else (client_id,)

                recent_sql = f"""
                    SELECT 
                        l.id, 
                        COALESCE(NULLIF(l.provider, ''), 'groq') as provider, 
                        l.model_name, 
                        l.prompt_tokens, 
                        l.completion_tokens, 
                        (l.cost * COALESCE(ea.cost_multiplier, 1.0)) as cost, 
                        l.latency_ms, 
                        l.caller_function, 
                        l.created_at,
                        l.email_log_id,
                        l.thread_id
                    FROM llm_logs l
                    LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                    {recent_logs_cond}
                    ORDER BY l.created_at DESC
                    LIMIT 30
                """
                if recent_logs_params:
                    cursor.execute(recent_sql, recent_logs_params)
                else:
                    cursor.execute(recent_sql)

                log_rows = cursor.fetchall()
                recent_logs = []
                for row in log_rows:
                    func_name = row[7]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    elif func_name == "llm_score":
                        func_display = "Output Quality Evaluation"
                    elif func_name == "generate_summary_llm":
                        func_display = "Thread Summarization"
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
                        "created_at": row[8].strftime("%b %d, %H:%M:%S") if row[8] else "",
                        "email_log_id": row[9],
                        "thread_id": row[10]
                    })

                # 8. Budget & Quota Status
                from app.email_credential import get_budget_status
                budget_info = get_budget_status(client_id, cursor)

                return {
                    "status": "success",
                    "totals": totals,
                    "providers": provider_breakdown,
                    "models": model_breakdown,
                    "callers": caller_breakdown,
                    "guardrail_analysis": guardrail_analysis,
                    "daily_trends": daily_trends,
                    "logs": recent_logs,
                    "budget": budget_info
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch LLM analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notifications/{client_id}")
def get_notifications_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    """
    Returns live operational alerts and system notifications for the given client or ALL tenants:
    - LLM Budget Quota warnings/exceeded
    - IMAP Ingestion Daemon stalls/delays
    - Recent failed deliveries and execution errors
    - Actionable human review requests (pending_manual_review)
    - High-urgency negative sentiment escalations
    """
    require_client_access(client_id, user)
    alerts = []

    try:
        import time
        from app.redis_pool import get_redis_main
        from app.email_credential import get_budget_status

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # 1. Budget & Quota Alerts
                try:
                    if client_id == "ALL":
                        # Check all accounts with budgets
                        cursor.execute("SELECT client_id, company_name, monthly_budget_usd FROM email_accounts WHERE monthly_budget_usd IS NOT NULL AND monthly_budget_usd > 0")
                        b_rows = cursor.fetchall()
                        for b_cid, b_cname, b_limit in b_rows:
                            b_stat = get_budget_status(b_cid, cursor)
                            b_spent = b_stat.get("spent", 0)
                            b_pct = b_stat.get("percent") or 0
                            disp_name = b_cname or b_cid
                            if b_stat.get("status") == "exceeded":
                                alerts.append({
                                    "id": f"budget-exceeded-{b_cid}",
                                    "category": "budget",
                                    "type": "error",
                                    "title": f"LLM Budget Exceeded ({disp_name})",
                                    "body": f"Spend has reached ${b_spent:.2f} of ${float(b_limit):.2f} monthly limit ({b_pct:.0f}%).",
                                    "time": "Active Limit",
                                    "client_id": b_cid,
                                    "action_url": f"/admin/clients"
                                })
                            elif b_stat.get("status") == "warning":
                                alerts.append({
                                    "id": f"budget-warning-{b_cid}",
                                    "category": "budget",
                                    "type": "warning",
                                    "title": f"LLM Quota Alert ({disp_name})",
                                    "body": f"Spend is at ${b_spent:.2f} of ${float(b_limit):.2f} monthly cap ({b_pct:.0f}%).",
                                    "time": "Current Month",
                                    "client_id": b_cid,
                                    "action_url": f"/admin/clients"
                                })
                    else:
                        b_stat = get_budget_status(client_id, cursor)
                        if b_stat.get("status") == "exceeded":
                            alerts.append({
                                "id": f"budget-exceeded-{client_id}",
                                "category": "budget",
                                "type": "error",
                                "title": "Monthly LLM Budget Exceeded",
                                "body": f"Spend has exceeded quota: ${b_stat['spent']:.2f} of ${b_stat['budget']:.2f} cap.",
                                "time": "Active Limit",
                                "client_id": client_id,
                                "action_url": "/dashboard?tab=llm"
                            })
                        elif b_stat.get("status") == "warning":
                            alerts.append({
                                "id": f"budget-warning-{client_id}",
                                "category": "budget",
                                "type": "warning",
                                "title": "LLM Budget Alert",
                                "body": f"Spend is at ${b_stat['spent']:.2f} of ${b_stat['budget']:.2f} cap ({b_stat['percent']}%%).",
                                "time": "Current Month",
                                "client_id": client_id,
                                "action_url": "/dashboard?tab=llm"
                            })
                except Exception as b_err:
                    logger.warning(f"⚠️ Notification budget check failed: {b_err}")

                # 2. Ingestion Daemon & Worker Liveness Alerts
                try:
                    r = get_redis_main()
                    if r:
                        now_ts = int(time.time())
                        hb_val = r.get("imap_worker:heartbeat")
                        if hb_val:
                            hb_diff = max(0, now_ts - int(hb_val))
                            if hb_diff > 180:
                                alerts.append({
                                    "id": f"worker-stalled",
                                    "category": "infrastructure",
                                    "type": "error",
                                    "title": "Ingestion Worker Stalled",
                                    "body": f"IMAP worker daemon last beat was {hb_diff // 60}m ago. Mail sync may be paused.",
                                    "time": f"{hb_diff // 60}m ago",
                                    "client_id": client_id,
                                    "action_url": "/dashboard"
                                })
                        if client_id != "ALL":
                            st_val = r.get(f"imap_status:{client_id}")
                            if st_val and "error" in st_val.lower():
                                alerts.append({
                                    "id": f"imap-error-{client_id}",
                                    "category": "mailbox",
                                    "type": "error",
                                    "title": "Mailbox Sync Error",
                                    "body": f"IMAP sync issue detected: {st_val}",
                                    "time": "Recent",
                                    "client_id": client_id,
                                    "action_url": "/accounts"
                                })
                except Exception as r_err:
                    logger.warning(f"⚠️ Notification Redis check failed: {r_err}")

                # 3. Actionable Human Review Backlog
                try:
                    col = "client_id"
                    try:
                        cursor.execute("DESCRIBE email_logs")
                        cols = [r[0] for r in cursor.fetchall()]
                        if "client_id" not in cols and "user_id" in cols:
                            col = "user_id"
                    except Exception:
                        pass

                    if client_id == "ALL":
                        cursor.execute(f"SELECT COUNT(*), MAX(created_at) FROM email_logs WHERE status = 'pending_manual_review'")
                    else:
                        cursor.execute(f"SELECT COUNT(*), MAX(created_at) FROM email_logs WHERE {col} = %s AND status = 'pending_manual_review'", (client_id,))
                    p_row = cursor.fetchone()
                    p_count = int(p_row[0] or 0) if p_row else 0
                    if p_count > 0:
                        alerts.append({
                            "id": f"review-backlog-{client_id}",
                            "category": "operations",
                            "type": "warning",
                            "title": f"{p_count} Draft{'s' if p_count > 1 else ''} Pending Review",
                            "body": f"{p_count} AI reply draft{'s require' if p_count > 1 else ' requires'} operator approval before dispatch.",
                            "time": "Awaiting Approval",
                            "client_id": client_id,
                            "action_url": "/drafts"
                        })
                except Exception as p_err:
                    logger.warning(f"⚠️ Notification review check failed: {p_err}")

                # 4. Recent Failed Pipeline Deliveries (Last 24 Hours)
                try:
                    fail_cond = "status IN ('failed', 'send_failed', 'ticket_creation_failed', 'ticket_created_send_failed') AND created_at >= NOW() - INTERVAL 24 HOUR"
                    if client_id == "ALL":
                        cursor.execute(f"""
                            SELECT id, from_email, subject, status, created_at, {col}
                            FROM email_logs 
                            WHERE {fail_cond}
                            ORDER BY created_at DESC 
                            LIMIT 5
                        """)
                    else:
                        cursor.execute(f"""
                            SELECT id, from_email, subject, status, created_at, {col}
                            FROM email_logs 
                            WHERE {col} = %s AND {fail_cond}
                            ORDER BY created_at DESC 
                            LIMIT 5
                        """, (client_id,))
                    fail_rows = cursor.fetchall()
                    for f_id, f_from, f_subj, f_st, f_dt, f_cid in fail_rows:
                        disp_time = f_dt.strftime("%I:%M %p") if f_dt else "Recent"
                        alerts.append({
                            "id": f"delivery-failed-{f_id}",
                            "category": "pipeline",
                            "type": "error",
                            "title": "Email Processing Failed",
                            "body": f"Failed to deliver response for '{f_subj or 'No Subject'}' from {f_from}.",
                            "time": disp_time,
                            "client_id": f_cid,
                            "email_id": f_id,
                            "action_url": f"/inbox?tab=failed&email_id={f_id}"
                        })
                except Exception as f_err:
                    logger.warning(f"⚠️ Notification failed rows query failed: {f_err}")

                # 5. Urgent Customer Sentiment Escalations (Last 24 Hours)
                try:
                    esc_cond = "sentiment IN ('Frustrated', 'Urgent', 'Negative') AND priority IN ('High', 'Critical') AND created_at >= NOW() - INTERVAL 24 HOUR"
                    if client_id == "ALL":
                        cursor.execute(f"""
                            SELECT id, from_email, subject, sentiment, priority, created_at, {col}
                            FROM email_logs 
                            WHERE {esc_cond}
                            ORDER BY created_at DESC 
                            LIMIT 3
                        """)
                    else:
                        cursor.execute(f"""
                            SELECT id, from_email, subject, sentiment, priority, created_at, {col}
                            FROM email_logs 
                            WHERE {col} = %s AND {esc_cond}
                            ORDER BY created_at DESC 
                            LIMIT 3
                        """, (client_id,))
                    esc_rows = cursor.fetchall()
                    for e_id, e_from, e_subj, e_sent, e_prio, e_dt, e_cid in esc_rows:
                        disp_time = e_dt.strftime("%I:%M %p") if e_dt else "Recent"
                        alerts.append({
                            "id": f"urgent-escalation-{e_id}",
                            "category": "sentiment",
                            "type": "warning",
                            "title": f"Urgent Customer Escalation ({e_prio})",
                            "body": f"{e_from}: {e_subj or 'Support Request'} ({e_sent} sentiment)",
                            "time": disp_time,
                            "client_id": e_cid,
                            "email_id": e_id,
                            "action_url": f"/inbox?email_id={e_id}"
                        })
                except Exception as e_err:
                    logger.warning(f"⚠️ Notification escalation rows query failed: {e_err}")

        return {
            "status": "success",
            "client_id": client_id,
            "total_alerts": len(alerts),
            "alerts": alerts
        }
    except Exception as ex:
        logger.error(f"❌ Failed to fetch notifications: {ex}", exc_info=True)
        return {
            "status": "error",
            "client_id": client_id,
            "total_alerts": 0,
            "alerts": []
        }

