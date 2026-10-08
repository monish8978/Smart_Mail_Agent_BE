import csv
from datetime import datetime
import io
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
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
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD INDEX idx_llm_thread (client_id, thread_id)")
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
            "chart_data": [],
            "inbound_health": {
                "total_inbound": 0,
                "real_inquiries": 0,
                "marketing_count": 0,
                "spam_count": 0,
                "blocked_count": 0,
                "bounce_count": 0,
                "bounce_rate": 0.0,
                "delivery_health_score": 100.0,
                "avg_daily_inbound": 0.0,
                "monthly_inbound_volume": 0,
                "hourly_profile": []
            },
            "execution_latency": {
                "total_execution_ms": 0,
                "avg_latency_ms": 0,
                "min_latency_ms": 0,
                "max_latency_ms": 0,
                "first_action": {
                    "avg_ms": 0,
                    "min_ms": 0,
                    "max_ms": 0
                }
            },
            "ingestion_health": {
                "worker_heartbeat": {"status": "offline", "last_seen_sec": None, "label": "Offline"},
                "queue_depth": 0,
                "oldest_pending_sec": 0,
                "client_sync": None
            },
            "conversational_performance": {
                "customer_resolution_rate": 0.0,
                "rag_resolved_count": 0,
                "ticket_escalated_count": 0,
                "fcr_count": 0,
                "fcr_rate": 0.0,
                "multi_turn_count": 0,
                "runaway_risk_count": 0,
                "avg_resolution_turns": 1.0,
                "avg_resolution_duration_sec": 0,
                "completed_conversations": {
                    "count": 0,
                    "avg_duration_sec": 0,
                    "min_duration_sec": 0,
                    "max_duration_sec": 0
                },
                "incomplete_conversations": {
                    "count": 0,
                    "total_duration_sec": 0,
                    "avg_duration_sec": 0,
                    "min_duration_sec": 0,
                    "max_duration_sec": 0
                },
                "rag_resolution_timing": {
                    "count": 0,
                    "avg_duration_sec": 0,
                    "min_duration_sec": 0,
                    "max_duration_sec": 0
                },
                "ticket_escalation_timing": {
                    "count": 0,
                    "avg_duration_sec": 0,
                    "min_duration_sec": 0,
                    "max_duration_sec": 0
                },
                "active_wip_conversations": 0,
                "avg_active_wip_age_sec": 0,
                "active_wip_aging": {
                    "under_1h": 0,
                    "warning_1_4h": 0,
                    "breached_over_4h": 0
                },
                "sentiment_trajectory": {
                    "improved": 0,
                    "neutral_stable": 0,
                    "deteriorated": 0
                },
                "sentiment_distribution": {
                    "positive": 0,
                    "neutral": 0,
                    "negative": 0,
                    "net_score": 0.0
                },
                "inferred_csat": 5.0,
                "intent_breakdown": [],
                "intent_matrix": []
            },
            "deltas": {
                "total_emails_pct": 0.0,
                "ai_replies_pct": 0.0,
                "automation_rate_pct": 0.0,
                "bounce_rate_pct": 0.0
            }
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
                           DATE_FORMAT(created_at, '{group_by_format}') as sort_key,
                           SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as clean_deflected,
                           SUM(CASE WHEN status IN ('ticket_created_and_sent', 'ticket_created_send_failed') THEN 1 ELSE 0 END) as escalated_tickets,
                           SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_review,
                           SUM(CASE WHEN status IN ('send_failed', 'pending_manual_review', 'ticket_creation_failed', 'bounced') THEN 1 ELSE 0 END) as failed_dropped
                    FROM email_logs
                    WHERE {where_clause}
                    GROUP BY sort_key, formatted_time
                    ORDER BY sort_key ASC
                """, tuple(chart_params))
                rows = cursor.fetchall()

                # 8a. Inbound Delivery Health & Traffic Filtering
                inbound_where = "WHERE 1=1" + date_filter if client_id == "ALL" else f"WHERE {col} = %s" + date_filter
                inbound_params = tuple(date_params) if client_id == "ALL" else (client_id, *date_params)

                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as total_inbound,
                        SUM(CASE WHEN LOWER(COALESCE(intent, '')) = 'marketing' 
                                      OR LOWER(COALESCE(sentiment, '')) = 'marketing' 
                                      OR LOWER(COALESCE(subject, '')) LIKE '%%unsubscribe%%' 
                                      OR LOWER(COALESCE(body, '')) LIKE '%%unsubscribe%%' THEN 1 ELSE 0 END) as marketing_count,
                        SUM(CASE WHEN status = 'spam' 
                                      OR LOWER(COALESCE(intent, '')) = 'spam'
                                      OR LOWER(COALESCE(subject, '')) LIKE '%%spam%%' THEN 1 ELSE 0 END) as spam_count,
                        SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked_count,
                        SUM(CASE WHEN status IN ('bounced', 'send_failed', 'ticket_created_send_failed') 
                                      OR LOWER(COALESCE(subject, '')) LIKE '%%mail delivery%%' 
                                      OR LOWER(COALESCE(subject, '')) LIKE '%%undelivered mail%%' 
                                      OR LOWER(COALESCE(subject, '')) LIKE '%%delivery status notification%%' THEN 1 ELSE 0 END) as bounce_count
                    FROM email_logs
                    {inbound_where}
                """, inbound_params)
                inbound_row = cursor.fetchone()
                total_in = int(inbound_row[0] or 0) if inbound_row else 0
                mkt_cnt = int(inbound_row[1] or 0) if inbound_row else 0
                spm_cnt = int(inbound_row[2] or 0) if inbound_row else 0
                blk_cnt = int(inbound_row[3] or 0) if inbound_row else 0
                bnc_cnt = int(inbound_row[4] or 0) if inbound_row else 0
                real_inq = max(0, total_in - (mkt_cnt + spm_cnt + blk_cnt + bnc_cnt))
                bnc_rate = round((bnc_cnt / total_in * 100), 1) if total_in > 0 else 0.0
                health_score = round(((total_in - bnc_cnt - blk_cnt - spm_cnt) / total_in * 100), 1) if total_in > 0 else 100.0

                # Velocity: Day-wise average & Monthly volume projection
                # Measure daily incoming across the last 30 days
                velocity_where = "WHERE created_at >= NOW() - INTERVAL 30 DAY" if client_id == "ALL" else f"WHERE {col} = %s AND created_at >= NOW() - INTERVAL 30 DAY"
                velocity_params = () if client_id == "ALL" else (client_id,)
                cursor.execute(f"""
                    SELECT 
                        COUNT(DISTINCT DATE(created_at)) as active_days,
                        COUNT(*) as total_month_volume
                    FROM email_logs
                    {velocity_where}
                """, velocity_params)
                v_row = cursor.fetchone()
                active_days = max(1, int(v_row[0] or 1)) if v_row else 1
                month_volume = int(v_row[1] or 0) if v_row else 0
                avg_daily_inbound = round(month_volume / float(active_days), 1)

                # 24-Hour Ingestion Cadence Profile across selected filter
                hourly_profile = []
                try:
                    cursor.execute(f"""
                        SELECT HOUR(created_at) as hr, COUNT(*) as cnt
                        FROM email_logs
                        {inbound_where}
                        GROUP BY hr
                        ORDER BY hr ASC
                    """, inbound_params)
                    h_map = {int(r[0]): int(r[1]) for r in cursor.fetchall() if r[0] is not None}
                    hourly_profile = [{"hour": h, "count": h_map.get(h, 0)} for h in range(24)]
                except Exception as hr_err:
                    logger.warning(f"⚠️ Failed to build hourly_profile: {hr_err}")

                stats_data["inbound_health"] = {
                    "total_inbound": total_in,
                    "real_inquiries": real_inq,
                    "marketing_count": mkt_cnt,
                    "spam_count": spm_cnt,
                    "blocked_count": blk_cnt,
                    "bounce_count": bnc_cnt,
                    "bounce_rate": bnc_rate,
                    "delivery_health_score": health_score,
                    "avg_daily_inbound": avg_daily_inbound,
                    "monthly_inbound_volume": month_volume,
                    "hourly_profile": hourly_profile
                }

                # 8b. Execution Latency Telemetry (Overall & First Action)
                llm_where = "WHERE 1=1" + date_filter.replace("created_at", "l.created_at") if client_id == "ALL" else f"WHERE l.client_id = %s" + date_filter.replace("created_at", "l.created_at")
                llm_params = tuple(date_params) if client_id == "ALL" else (client_id, *date_params)
                cursor.execute(f"""
                    SELECT 
                        COALESCE(SUM(l.latency_ms), 0) as total_exec_ms,
                        COALESCE(AVG(l.latency_ms), 0) as avg_latency_ms,
                        COALESCE(MIN(l.latency_ms), 0) as min_latency_ms,
                        COALESCE(MAX(l.latency_ms), 0) as max_latency_ms
                    FROM llm_logs l
                    {llm_where}
                """, llm_params)
                lat_row = cursor.fetchone()
                tot_exec_ms = int(lat_row[0] or 0) if lat_row else 0
                avg_exec_ms = int(round(float(lat_row[1] or 0))) if lat_row else 0
                min_exec_ms = int(lat_row[2] or 0) if lat_row else 0
                max_exec_ms = int(lat_row[3] or 0) if lat_row else 0

                # First action latency on mail: single-turn inquiry or first turn of thread
                cursor.execute(f"""
                    SELECT 
                        COALESCE(AVG(l.latency_ms), 0) as avg_fa_ms,
                        COALESCE(MIN(l.latency_ms), 0) as min_fa_ms,
                        COALESCE(MAX(l.latency_ms), 0) as max_fa_ms
                    FROM llm_logs l
                    JOIN email_logs e ON l.email_log_id = e.id
                    {llm_where.replace('WHERE ', 'WHERE (e.troubleshooting_step <= 1 OR e.troubleshooting_step IS NULL) AND ')}
                """, llm_params)
                fa_row = cursor.fetchone()
                fa_avg_ms = int(round(float(fa_row[0] or avg_exec_ms))) if fa_row else avg_exec_ms
                fa_min_ms = int(fa_row[1] or min_exec_ms) if fa_row else min_exec_ms
                fa_max_ms = int(fa_row[2] or max_exec_ms) if fa_row else max_exec_ms

                stats_data["execution_latency"] = {
                    "total_execution_ms": tot_exec_ms,
                    "avg_latency_ms": avg_exec_ms,
                    "min_latency_ms": min_exec_ms,
                    "max_latency_ms": max_exec_ms,
                    "first_action": {
                        "avg_ms": fa_avg_ms,
                        "min_ms": fa_min_ms,
                        "max_ms": fa_max_ms
                    }
                }

                # 8c. Conversational CRR & Deflection Performance
                cursor.execute(f"""
                    SELECT 
                        SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') AND (escalation_reason IS NULL OR escalation_reason = '') THEN 1 ELSE 0 END) as rag_deflected,
                        SUM(CASE WHEN status LIKE '%%ticket%%' OR (escalation_reason IS NOT NULL AND escalation_reason != '') THEN 1 ELSE 0 END) as escalated
                    FROM email_logs
                    {inbound_where}
                """, inbound_params)
                res_row = cursor.fetchone()
                rag_resolved = int(res_row[0] or 0) if res_row else 0
                ticket_escalated = int(res_row[1] or 0) if res_row else 0

                total_handled = rag_resolved + ticket_escalated
                crr_rate = round((rag_resolved / total_handled * 100), 1) if total_handled > 0 else (100.0 if rag_resolved > 0 else 0.0)

                # Completed vs Incomplete Thread Analysis
                thread_where = "WHERE thread_id IS NOT NULL" + date_filter if client_id == "ALL" else f"WHERE {col} = %s AND thread_id IS NOT NULL" + date_filter
                
                # Completed Threads: closed or resolved
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as completed_count,
                        COALESCE(AVG(duration_sec), 0) as avg_duration,
                        COALESCE(MIN(duration_sec), 0) as min_duration,
                        COALESCE(MAX(duration_sec), 0) as max_duration,
                        COALESCE(AVG(turns), 1.0) as avg_turns
                    FROM (
                        SELECT 
                            thread_id, 
                            COUNT(*) as turns,
                            TIMESTAMPDIFF(SECOND, MIN(created_at), MAX(created_at)) as duration_sec
                        FROM email_logs
                        {thread_where}
                        GROUP BY thread_id
                        HAVING MAX(conversation_closed) = 1 
                            OR MAX(is_resolved) = 1 
                            OR MAX(status IN ('sent', 'ticket_created_and_sent')) = 1
                    ) ct
                """, inbound_params)
                ct_row = cursor.fetchone()
                completed_threads = {
                    "count": int(ct_row[0] or 0) if ct_row else 0,
                    "avg_duration_sec": int(ct_row[1] or 0) if ct_row else 0,
                    "min_duration_sec": int(ct_row[2] or 0) if ct_row else 0,
                    "max_duration_sec": int(ct_row[3] or 0) if ct_row else 0
                }
                avg_turns = round(float(ct_row[4] or 1.0), 1) if ct_row and ct_row[4] else 1.0

                # Incomplete Threads (Active / Open / In-flight)
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as incomplete_count,
                        COALESCE(SUM(age_sec), 0) as total_age_sec,
                        COALESCE(AVG(age_sec), 0) as avg_age_sec,
                        COALESCE(MIN(age_sec), 0) as min_age_sec,
                        COALESCE(MAX(age_sec), 0) as max_age_sec
                    FROM (
                        SELECT 
                            thread_id,
                            TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()) as age_sec
                        FROM email_logs
                        {thread_where}
                        GROUP BY thread_id
                        HAVING MAX(conversation_closed) = 0 
                           AND MAX(is_resolved) = 0
                           AND MAX(status) = 'pending'
                    ) it
                """, inbound_params)
                it_row = cursor.fetchone()
                incomplete_threads = {
                    "count": int(it_row[0] or 0) if it_row else 0,
                    "total_duration_sec": int(it_row[1] or 0) if it_row else 0,
                    "avg_duration_sec": int(it_row[2] or 0) if it_row else 0,
                    "min_duration_sec": int(it_row[3] or 0) if it_row else 0,
                    "max_duration_sec": int(it_row[4] or 0) if it_row else 0
                }

                # RAG Resolution Timing (threads deflected without raising a ticket)
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as rag_count,
                        COALESCE(AVG(duration_sec), 0) as avg_dur,
                        COALESCE(MIN(duration_sec), 0) as min_dur,
                        COALESCE(MAX(duration_sec), 0) as max_dur
                    FROM (
                        SELECT 
                            thread_id,
                            TIMESTAMPDIFF(SECOND, MIN(created_at), MAX(created_at)) as duration_sec
                        FROM email_logs
                        {thread_where}
                        GROUP BY thread_id
                        HAVING SUM(CASE WHEN status LIKE '%%ticket%%' OR (escalation_reason IS NOT NULL AND escalation_reason != '') THEN 1 ELSE 0 END) = 0
                           AND MAX(status IN ('sent', 'ticket_created_and_sent')) = 1
                    ) rt
                """, inbound_params)
                rt_row = cursor.fetchone()
                rag_timing = {
                    "count": int(rt_row[0] or rag_resolved) if rt_row else rag_resolved,
                    "avg_duration_sec": int(rt_row[1] or 0) if rt_row else 0,
                    "min_duration_sec": int(rt_row[2] or 0) if rt_row else 0,
                    "max_duration_sec": int(rt_row[3] or 0) if rt_row else 0
                }

                # Ticket Escalation Timing (threads that resulted in a ticket)
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as ticket_count,
                        COALESCE(AVG(duration_sec), 0) as avg_dur,
                        COALESCE(MIN(duration_sec), 0) as min_dur,
                        COALESCE(MAX(duration_sec), 0) as max_dur
                    FROM (
                        SELECT 
                            thread_id,
                            TIMESTAMPDIFF(SECOND, MIN(created_at), MAX(created_at)) as duration_sec
                        FROM email_logs
                        {thread_where}
                        GROUP BY thread_id
                        HAVING SUM(CASE WHEN status LIKE '%%ticket%%' OR (escalation_reason IS NOT NULL AND escalation_reason != '') THEN 1 ELSE 0 END) > 0
                    ) tt
                """, inbound_params)
                tt_row = cursor.fetchone()
                ticket_timing = {
                    "count": int(tt_row[0] or ticket_escalated) if tt_row else ticket_escalated,
                    "avg_duration_sec": int(tt_row[1] or 0) if tt_row else 0,
                    "min_duration_sec": int(tt_row[2] or 0) if tt_row else 0,
                    "max_duration_sec": int(tt_row[3] or 0) if tt_row else 0
                }

                # Active WIP conversations (open threads)
                wip_where = "WHERE conversation_closed = 0 AND status = 'pending'" if client_id == "ALL" else f"WHERE {col} = %s AND conversation_closed = 0 AND status = 'pending'"
                cursor.execute(f"""
                    SELECT 
                        COUNT(DISTINCT thread_id),
                        COALESCE(AVG(TIMESTAMPDIFF(SECOND, created_at, NOW())), 0)
                    FROM email_logs
                    {wip_where}
                """, tuple(date_params) if client_id == "ALL" else (client_id,))
                wip_row = cursor.fetchone()
                active_wip = int(wip_row[0] or 0) if wip_row else 0
                avg_wip_age = int(wip_row[1] or 0) if wip_row else 0

                # Sentiment trajectory & full conversation sentiment distribution
                cursor.execute(f"""
                    SELECT 
                        SUM(CASE WHEN LOWER(sentiment) IN ('positive', 'delighted') THEN 1 ELSE 0 END) as positive_cnt,
                        SUM(CASE WHEN LOWER(sentiment) IN ('neutral', 'mixed') THEN 1 ELSE 0 END) as neutral_cnt,
                        SUM(CASE WHEN LOWER(sentiment) IN ('frustrated', 'urgent', 'negative') THEN 1 ELSE 0 END) as negative_cnt
                    FROM email_logs
                    {inbound_where}
                """, inbound_params)
                sent_row = cursor.fetchone()
                pos_cnt = int(sent_row[0] or 0) if sent_row else 0
                neu_cnt = int(sent_row[1] or 0) if sent_row else 0
                neg_cnt = int(sent_row[2] or 0) if sent_row else 0
                total_sent = pos_cnt + neu_cnt + neg_cnt
                net_sentiment_score = round(((pos_cnt - neg_cnt) / max(1, total_sent)) * 100, 1)

                # Inferred CSAT calculation
                if total_in > 0:
                    raw_csat = 3.2 + (1.3 * (crr_rate / 100.0)) + (0.5 * (pos_cnt / max(1, pos_cnt + neg_cnt))) - (0.8 * (neg_cnt / total_in))
                    inferred_csat = round(max(1.0, min(5.0, raw_csat)), 2)
                else:
                    inferred_csat = 5.0

                # First-Contact Resolution (FCR) vs Multi-Turn Resolution
                cursor.execute(f"""
                    SELECT 
                        SUM(CASE WHEN turns = 1 THEN 1 ELSE 0 END) as fcr_cnt,
                        SUM(CASE WHEN turns BETWEEN 2 AND 4 THEN 1 ELSE 0 END) as multi_cnt,
                        SUM(CASE WHEN turns >= 5 THEN 1 ELSE 0 END) as runaway_cnt
                    FROM (
                        SELECT 
                            thread_id, 
                            COUNT(*) as turns,
                            SUM(CASE WHEN status LIKE '%%ticket%%' OR (escalation_reason IS NOT NULL AND escalation_reason != '') THEN 1 ELSE 0 END) as escalations
                        FROM email_logs
                        {thread_where}
                        GROUP BY thread_id
                        HAVING escalations = 0
                    ) resolved_threads
                """, inbound_params)
                fcr_row = cursor.fetchone()
                fcr_cnt = int(fcr_row[0] or 0) if fcr_row else 0
                multi_cnt = int(fcr_row[1] or 0) if fcr_row else 0
                runaway_cnt = int(fcr_row[2] or 0) if fcr_row else 0
                fcr_pct = round((fcr_cnt / max(1, rag_resolved) * 100), 1) if rag_resolved > 0 else 0.0

                # Active WIP Aging Buckets
                cursor.execute(f"""
                    SELECT 
                        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, created_at, NOW()) < 3600 THEN 1 ELSE 0 END) as under_1h,
                        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, created_at, NOW()) BETWEEN 3600 AND 14400 THEN 1 ELSE 0 END) as warning_1_4h,
                        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, created_at, NOW()) > 14400 THEN 1 ELSE 0 END) as breached_over_4h
                    FROM email_logs
                    {wip_where}
                """, tuple(date_params) if client_id == "ALL" else (client_id,))
                wip_aging_row = cursor.fetchone()
                wip_aging = {
                    "under_1h": int(wip_aging_row[0] or 0) if wip_aging_row else 0,
                    "warning_1_4h": int(wip_aging_row[1] or 0) if wip_aging_row else 0,
                    "breached_over_4h": int(wip_aging_row[2] or 0) if wip_aging_row else 0
                }

                # Intent Automation & Deflection Matrix
                cursor.execute(f"""
                    SELECT 
                        COALESCE(NULLIF(intent, ''), 'general_inquiry') as intent_name,
                        COUNT(*) as total_volume,
                        SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as auto_sent,
                        SUM(CASE WHEN status LIKE '%%ticket%%' OR (escalation_reason IS NOT NULL AND escalation_reason != '') THEN 1 ELSE 0 END) as tickets,
                        COALESCE(AVG(CASE WHEN thread_id IS NOT NULL THEN 1.2 ELSE 1.0 END), 1.0) as avg_turns,
                        COALESCE(NULLIF(SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(NULLIF(escalation_reason, ''), 'none') ORDER BY id DESC SEPARATOR ','), ',', 1), 'none'), '-') as latest_reason
                    FROM email_logs
                    {inbound_where}
                    GROUP BY intent_name
                    ORDER BY total_volume DESC
                    LIMIT 8
                """, inbound_params)
                intent_matrix_rows = cursor.fetchall()
                intent_matrix = []
                for im in intent_matrix_rows:
                    t_vol = int(im[1] or 0)
                    a_sent = int(im[2] or 0)
                    t_cnt = int(im[3] or 0)
                    intent_matrix.append({
                        "intent": im[0],
                        "total_volume": t_vol,
                        "auto_sent_count": a_sent,
                        "auto_sent_rate": round(a_sent / t_vol * 100, 1) if t_vol > 0 else 0.0,
                        "ticket_count": t_cnt,
                        "escalation_rate": round(t_cnt / t_vol * 100, 1) if t_vol > 0 else 0.0,
                        "avg_turns": round(float(im[4] or 1.0), 1),
                        "primary_escalation_reason": im[5] if im[5] != 'none' else '-'
                    })

                # Ticket creation breakdown by technical intent
                ticket_intent_where = "WHERE 1=1" + date_filter if client_id == "ALL" else f"WHERE client_id = %s" + date_filter
                cursor.execute(f"""
                    SELECT 
                        COALESCE(NULLIF(intent, ''), 'general_inquiry') as intent_name,
                        COUNT(*) as ticket_count
                    FROM ticket_record
                    {ticket_intent_where}
                    GROUP BY intent_name
                    ORDER BY ticket_count DESC
                    LIMIT 10
                """, inbound_params)
                intent_rows = cursor.fetchall()
                intent_breakdown = [{"intent": r[0], "count": int(r[1])} for r in intent_rows]

                stats_data["conversational_performance"] = {
                    "customer_resolution_rate": crr_rate,
                    "rag_resolved_count": rag_resolved,
                    "ticket_escalated_count": ticket_escalated,
                    "fcr_count": fcr_cnt,
                    "fcr_rate": fcr_pct,
                    "multi_turn_count": multi_cnt,
                    "runaway_risk_count": runaway_cnt,
                    "avg_resolution_turns": avg_turns,
                    "avg_resolution_duration_sec": completed_threads["avg_duration_sec"],
                    "completed_conversations": completed_threads,
                    "incomplete_conversations": incomplete_threads,
                    "rag_resolution_timing": rag_timing,
                    "ticket_escalation_timing": ticket_timing,
                    "active_wip_conversations": active_wip,
                    "avg_active_wip_age_sec": avg_wip_age,
                    "active_wip_aging": wip_aging,
                    "sentiment_trajectory": {
                        "improved": pos_cnt,
                        "neutral_stable": neu_cnt,
                        "deteriorated": neg_cnt
                    },
                    "sentiment_distribution": {
                        "positive": pos_cnt,
                        "neutral": neu_cnt,
                        "negative": neg_cnt,
                        "net_score": net_sentiment_score
                    },
                    "inferred_csat": inferred_csat,
                    "intent_breakdown": intent_breakdown,
                    "intent_matrix": intent_matrix
                }

                # 8d. Period-over-Period Delta Calculation
                prior_interval = None
                prior_params = []
                if range_type == "today":
                    prior_interval = "DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"
                elif range_type == "yesterday":
                    prior_interval = "DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 2 DAY)"
                elif range_type == "this_month":
                    prior_interval = "MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))"
                elif range_type == "last_month":
                    prior_interval = "MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 2 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 2 MONTH))"
                elif range_type == "custom" and start_date and end_date:
                    try:
                        from datetime import datetime, timedelta
                        s_dt = datetime.strptime(start_date, "%Y-%m-%d")
                        e_dt = datetime.strptime(end_date, "%Y-%m-%d")
                        duration = (e_dt - s_dt).days + 1
                        prior_e = s_dt - timedelta(days=1)
                        prior_s = prior_e - timedelta(days=duration - 1)
                        prior_interval = "DATE(created_at) BETWEEN %s AND %s"
                        prior_params = [prior_s.strftime("%Y-%m-%d"), prior_e.strftime("%Y-%m-%d")]
                    except Exception:
                        prior_interval = None

                deltas = {
                    "total_emails_pct": 0.0,
                    "ai_replies_pct": 0.0,
                    "automation_rate_pct": 0.0,
                    "bounce_rate_pct": 0.0
                }

                if prior_interval:
                    prior_where = f"WHERE {prior_interval}" if client_id == "ALL" else f"WHERE {col} = %s AND {prior_interval}"
                    p_query_params = tuple(prior_params) if client_id == "ALL" else (client_id, *prior_params)
                    try:
                        cursor.execute(f"""
                            SELECT 
                                COUNT(*) as prior_total,
                                SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as prior_replies,
                                SUM(CASE WHEN status IN ('bounced', 'send_failed', 'ticket_created_send_failed') THEN 1 ELSE 0 END) as prior_bounces
                            FROM email_logs
                            {prior_where}
                        """, p_query_params)
                        p_row = cursor.fetchone()
                        if p_row:
                            p_tot = int(p_row[0] or 0)
                            p_rep = int(p_row[1] or 0)
                            p_bnc = int(p_row[2] or 0)
                            p_auto_rate = round(p_rep / p_tot * 100, 1) if p_tot > 0 else 0.0
                            p_bnc_rate = round(p_bnc / p_tot * 100, 1) if p_tot > 0 else 0.0

                            curr_tot = stats_data["total_emails"]
                            curr_rep = stats_data["ai_replies"]
                            curr_auto_rate = round(curr_rep / curr_tot * 100, 1) if curr_tot > 0 else 0.0

                            def calc_pct(curr, prev):
                                if prev == 0:
                                    return 0.0 if curr == 0 else 100.0
                                return round(((curr - prev) / prev) * 100, 1)

                            deltas = {
                                "total_emails_pct": calc_pct(curr_tot, p_tot),
                                "ai_replies_pct": calc_pct(curr_rep, p_rep),
                                "automation_rate_pct": round(curr_auto_rate - p_auto_rate, 1),
                                "bounce_rate_pct": round(bnc_rate - p_bnc_rate, 1)
                            }
                    except Exception as p_err:
                        logger.warning(f"⚠️ Prior delta calculation skipped: {p_err}")

                stats_data["deltas"] = deltas

        chart_data = []
        for row in rows:
            chart_data.append({
                "name": row[0] if len(row) > 0 else "",
                "emails": int(row[1] or 0) if len(row) > 1 else 0,
                "aiReplied": int(row[2] or 0) if len(row) > 2 else 0,
                "deflected": int(row[4] or 0) if len(row) > 4 else (int(row[2] or 0) if len(row) > 2 else 0),
                "escalated": int(row[5] or 0) if len(row) > 5 else 0,
                "pending": int(row[6] or 0) if len(row) > 6 else 0,
                "failed": int(row[7] or 0) if len(row) > 7 else 0
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

            # 10. Celery queue depth and ingestion latency
            queue_depth = 0
            try:
                queue_depth = int(r.llen("celery") or 0)
            except Exception:
                queue_depth = 0

            stats_data["ingestion_health"] = {
                "worker_heartbeat": stats_data.get("worker_heartbeat", {"status": "offline", "last_seen_sec": None, "label": "Offline"}),
                "queue_depth": queue_depth,
                "oldest_pending_sec": stats_data.get("oldest_pending_seconds", 0),
                "client_sync": stats_data.get("client_sync")
            }
        except Exception as r_err:
            logger.warning(f"⚠️ Redis worker heartbeat query failed: {r_err}")
            stats_data["worker_heartbeat"] = {"status": "unknown", "last_seen_sec": None, "label": "Unknown"}
            stats_data["client_sync"] = None
            stats_data["ingestion_health"] = {
                "worker_heartbeat": {"status": "unknown", "last_seen_sec": None, "label": "Unknown"},
                "queue_depth": 0,
                "oldest_pending_sec": stats_data.get("oldest_pending_seconds", 0),
                "client_sync": None
            }

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

                # 7c. Token Economics Calculation (Grouped by Email & Conversation Lifecycle)
                email_eco_where = f"WHERE 1=1 {time_filter}" if client_id == "ALL" else f"WHERE l.client_id = %s {time_filter}"
                email_eco_params = () if client_id == "ALL" else (client_id,)

                # 7c-1: Per-Email Unit Economics (Grouped by email_log_id or fallback to individual request if NULL)
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as total_emails_processed,
                        COALESCE(SUM(mail_cost), 0) as total_mail_cost,
                        COALESCE(AVG(mail_cost), 0) as avg_mail_cost,
                        COALESCE(MIN(NULLIF(mail_cost, 0)), 0) as min_mail_cost,
                        COALESCE(MAX(mail_cost), 0) as max_mail_cost,
                        COALESCE(SUM(mail_tokens), 0) as total_mail_tokens,
                        COALESCE(AVG(mail_tokens), 0) as avg_mail_tokens,
                        COALESCE(MIN(NULLIF(mail_tokens, 0)), 0) as min_mail_tokens,
                        COALESCE(MAX(mail_tokens), 0) as max_mail_tokens,
                        COALESCE(AVG(api_invocations), 1.0) as avg_api_calls_per_mail,
                        COALESCE(MIN(api_invocations), 1) as min_api_calls_per_mail,
                        COALESCE(MAX(api_invocations), 1) as max_api_calls_per_mail
                    FROM (
                        SELECT 
                            COALESCE(l.email_log_id, l.id) as mail_key,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as mail_cost,
                            SUM(l.prompt_tokens + l.completion_tokens) as mail_tokens,
                            COUNT(l.id) as api_invocations
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        {email_eco_where}
                        GROUP BY COALESCE(l.email_log_id, l.id)
                    ) m
                """, email_eco_params)
                m_row = cursor.fetchone()
                total_emails_processed = int(m_row[0] or 0) if m_row else 0
                total_mail_cost = float(m_row[1] or 0.0) if m_row else 0.0
                avg_cost_per_email = round(float(m_row[2] or 0.0), 5) if m_row else 0.0
                min_cost_per_email = round(float(m_row[3] or 0.0), 5) if m_row else 0.0
                max_cost_per_email = round(float(m_row[4] or 0.0), 5) if m_row else 0.0
                total_mail_tokens = int(m_row[5] or 0) if m_row else 0
                avg_tokens_per_email = round(float(m_row[6] or 0.0), 1) if m_row else 0.0
                min_tokens_per_email = int(m_row[7] or 0) if m_row else 0
                max_tokens_per_email = int(m_row[8] or 0) if m_row else 0
                avg_calls_per_email = round(float(m_row[9] or 1.0), 2) if m_row else 1.0
                min_calls_per_email = int(m_row[10] or 1) if m_row else 1
                max_calls_per_email = int(m_row[11] or 1) if m_row else 1

                # 7c-2: Completed Conversation Unit Economics
                completed_conv_where = f"WHERE l.thread_id IS NOT NULL {time_filter}" if client_id == "ALL" else f"WHERE l.client_id = %s AND l.thread_id IS NOT NULL {time_filter}"
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as total_completed_conversations,
                        COALESCE(SUM(t_cost), 0) as total_completed_cost,
                        COALESCE(AVG(t_cost), 0) as avg_completed_cost,
                        COALESCE(MIN(NULLIF(t_cost, 0)), 0) as min_completed_cost,
                        COALESCE(MAX(t_cost), 0) as max_completed_cost,
                        COALESCE(SUM(t_tokens), 0) as total_completed_tokens,
                        COALESCE(AVG(t_tokens), 0) as avg_completed_tokens,
                        COALESCE(MIN(NULLIF(t_tokens, 0)), 0) as min_completed_tokens,
                        COALESCE(MAX(t_tokens), 0) as max_completed_tokens,
                        COALESCE(AVG(t_turns), 1.0) as avg_completed_turns,
                        COALESCE(MIN(t_turns), 1) as min_completed_turns,
                        COALESCE(MAX(t_turns), 1) as max_completed_turns,
                        COALESCE(AVG(t_invocations), 1.0) as avg_invocations_per_thread,
                        COALESCE(MIN(t_invocations), 1) as min_invocations_per_thread,
                        COALESCE(MAX(t_invocations), 1) as max_invocations_per_thread
                    FROM (
                        SELECT 
                            l.thread_id,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as t_cost,
                            SUM(l.prompt_tokens + l.completion_tokens) as t_tokens,
                            COUNT(DISTINCT COALESCE(l.email_log_id, l.id)) as t_turns,
                            COUNT(l.id) as t_invocations
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        LEFT JOIN email_logs e ON l.email_log_id = e.id
                        {completed_conv_where}
                        GROUP BY l.thread_id
                        HAVING MAX(e.conversation_closed) = 1 
                            OR MAX(e.is_resolved) = 1 
                            OR MAX(e.status IN ('sent', 'ticket_created_and_sent')) = 1
                    ) tc
                """, email_eco_params)
                c_row = cursor.fetchone()
                completed_conv = {
                    "total_count": int(c_row[0] or 0) if c_row else 0,
                    "total_cost": round(float(c_row[1] or 0.0), 5) if c_row else 0.0,
                    "avg_cost": round(float(c_row[2] or 0.0), 5) if c_row else 0.0,
                    "min_cost": round(float(c_row[3] or 0.0), 5) if c_row else 0.0,
                    "max_cost": round(float(c_row[4] or 0.0), 5) if c_row else 0.0,
                    "total_tokens": int(c_row[5] or 0) if c_row else 0,
                    "avg_tokens": round(float(c_row[6] or 0.0), 1) if c_row else 0.0,
                    "min_tokens": int(c_row[7] or 0) if c_row else 0,
                    "max_tokens": int(c_row[8] or 0) if c_row else 0,
                    "avg_turns": round(float(c_row[9] or 1.0), 1) if c_row else 1.0,
                    "min_turns": int(c_row[10] or 1) if c_row else 1,
                    "max_turns": int(c_row[11] or 1) if c_row else 1,
                    "avg_api_invocations": round(float(c_row[12] or 1.0), 1) if c_row else 1.0,
                    "min_api_invocations": int(c_row[13] or 1) if c_row else 1,
                    "max_api_invocations": int(c_row[14] or 1) if c_row else 1
                }

                # 7c-3: Incomplete / Active In-Flight (WIP) Conversation Unit Economics
                cursor.execute(f"""
                    SELECT 
                        COUNT(*) as total_incomplete_conversations,
                        COALESCE(SUM(t_cost), 0) as total_incomplete_cost,
                        COALESCE(AVG(t_cost), 0) as avg_incomplete_cost,
                        COALESCE(MIN(NULLIF(t_cost, 0)), 0) as min_incomplete_cost,
                        COALESCE(MAX(t_cost), 0) as max_incomplete_cost,
                        COALESCE(SUM(t_tokens), 0) as total_incomplete_tokens,
                        COALESCE(AVG(t_tokens), 0) as avg_incomplete_tokens,
                        COALESCE(MIN(NULLIF(t_tokens, 0)), 0) as min_incomplete_tokens,
                        COALESCE(MAX(t_tokens), 0) as max_incomplete_tokens,
                        COALESCE(AVG(t_turns), 1.0) as avg_incomplete_turns,
                        COALESCE(MIN(t_turns), 1) as min_incomplete_turns,
                        COALESCE(MAX(t_turns), 1) as max_incomplete_turns,
                        COALESCE(AVG(t_invocations), 1.0) as avg_invocations_per_thread,
                        COALESCE(MIN(t_invocations), 1) as min_invocations_per_thread,
                        COALESCE(MAX(t_invocations), 1) as max_invocations_per_thread
                    FROM (
                        SELECT 
                            l.thread_id,
                            SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as t_cost,
                            SUM(l.prompt_tokens + l.completion_tokens) as t_tokens,
                            COUNT(DISTINCT COALESCE(l.email_log_id, l.id)) as t_turns,
                            COUNT(l.id) as t_invocations
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        LEFT JOIN email_logs e ON l.email_log_id = e.id
                        {completed_conv_where}
                        GROUP BY l.thread_id
                        HAVING (MAX(e.conversation_closed) = 0 OR MAX(e.conversation_closed) IS NULL)
                           AND (MAX(e.is_resolved) = 0 OR MAX(e.is_resolved) IS NULL)
                           AND MAX(e.status) = 'pending'
                    ) ic
                """, email_eco_params)
                ic_row = cursor.fetchone()
                incomplete_conv = {
                    "total_count": int(ic_row[0] or 0) if ic_row else 0,
                    "total_cost": round(float(ic_row[1] or 0.0), 5) if ic_row else 0.0,
                    "avg_cost": round(float(ic_row[2] or 0.0), 5) if ic_row else 0.0,
                    "min_cost": round(float(ic_row[3] or 0.0), 5) if ic_row else 0.0,
                    "max_cost": round(float(ic_row[4] or 0.0), 5) if ic_row else 0.0,
                    "total_tokens": int(ic_row[5] or 0) if ic_row else 0,
                    "avg_tokens": round(float(ic_row[6] or 0.0), 1) if ic_row else 0.0,
                    "min_tokens": int(ic_row[7] or 0) if ic_row else 0,
                    "max_tokens": int(ic_row[8] or 0) if ic_row else 0,
                    "avg_turns": round(float(ic_row[9] or 1.0), 1) if ic_row else 1.0,
                    "min_turns": int(ic_row[10] or 1) if ic_row else 1,
                    "max_turns": int(ic_row[11] or 1) if ic_row else 1,
                    "avg_api_invocations": round(float(ic_row[12] or 1.0), 1) if ic_row else 1.0,
                    "min_api_invocations": int(ic_row[13] or 1) if ic_row else 1,
                    "max_api_invocations": int(ic_row[14] or 1) if ic_row else 1
                }

                # Runaway alert for incomplete threads spending more than 2x average completed conversation cost
                threshold_cost = (completed_conv["avg_cost"] * 2.0) if completed_conv["avg_cost"] > 0 else 0.05
                runaway_threads_count = 0
                if incomplete_conv["total_count"] > 0:
                    cursor.execute(f"""
                        SELECT COUNT(*)
                        FROM (
                            SELECT l.thread_id, SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as t_cost
                            FROM llm_logs l
                            LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                            LEFT JOIN email_logs e ON l.email_log_id = e.id
                            {completed_conv_where}
                            GROUP BY l.thread_id
                            HAVING (MAX(e.conversation_closed) = 0 OR MAX(e.conversation_closed) IS NULL)
                               AND (MAX(e.is_resolved) = 0 OR MAX(e.is_resolved) IS NULL)
                               AND MAX(e.status) = 'pending'
                               AND t_cost > %s
                        ) rw
                    """, (*email_eco_params, threshold_cost))
                    runaway_rw = cursor.fetchone()
                    runaway_threads_count = int(runaway_rw[0] or 0) if runaway_rw else 0

                incomplete_conv["runaway_risk_count"] = runaway_threads_count

                # All conversation aggregates (fallback compatibility)
                total_convs = completed_conv["total_count"] + incomplete_conv["total_count"]
                avg_cost_conv = completed_conv["avg_cost"] if completed_conv["total_count"] > 0 else avg_cost_per_email
                avg_tokens_conv = completed_conv["avg_tokens"] if completed_conv["total_count"] > 0 else avg_tokens_per_email
                avg_turns_conv = completed_conv["avg_turns"]

                # Intent breakdown for token economics
                intent_eco_where = f"WHERE 1=1 {time_filter}" if client_id == "ALL" else f"WHERE l.client_id = %s {time_filter}"
                cursor.execute(f"""
                    SELECT 
                        COALESCE(NULLIF(e.intent, ''), 'general_inquiry') as intent_name,
                        COUNT(DISTINCT COALESCE(l.thread_id, l.id)) as conv_count,
                        COALESCE(SUM(l.prompt_tokens + l.completion_tokens), 0) as total_tokens,
                        COALESCE(SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)), 0) as total_cost,
                        COALESCE(AVG(l.cost * COALESCE(ea.cost_multiplier, 1.0)), 0) as avg_cost
                    FROM llm_logs l
                    LEFT JOIN email_logs e ON l.email_log_id = e.id
                    LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                    {intent_eco_where}
                    GROUP BY intent_name
                    ORDER BY total_cost DESC
                    LIMIT 15
                """, email_eco_params)
                intent_eco_rows = cursor.fetchall()
                economics_by_intent = [
                    {
                        "intent": r[0],
                        "conversations": int(r[1] or 0),
                        "total_tokens": int(r[2] or 0),
                        "total_cost": round(float(r[3] or 0.0), 5),
                        "avg_cost": round(float(r[4] or 0.0), 5)
                    }
                    for r in intent_eco_rows
                ]

                token_economics = {
                    "per_email": {
                        "total_emails": total_emails_processed,
                        "total_cost": total_mail_cost,
                        "avg_cost": avg_cost_per_email,
                        "min_cost": min_cost_per_email,
                        "max_cost": max_cost_per_email,
                        "total_tokens": total_mail_tokens,
                        "avg_tokens": avg_tokens_per_email,
                        "min_tokens": min_tokens_per_email,
                        "max_tokens": max_tokens_per_email,
                        "api_invocations": {
                            "total": totals["total_requests"],
                            "avg_per_mail": avg_calls_per_email,
                            "min_per_mail": min_calls_per_email,
                            "max_per_mail": max_calls_per_email
                        }
                    },
                    "completed_conversations": completed_conv,
                    "incomplete_conversations": incomplete_conv,
                    "avg_cost_per_email": avg_cost_per_email,
                    "avg_tokens_per_email": avg_tokens_per_email,
                    "avg_cost_per_conversation": avg_cost_conv,
                    "avg_tokens_per_conversation": avg_tokens_conv,
                    "avg_turns_per_conversation": avg_turns_conv,
                    "total_conversations": total_convs,
                    "guardrail_overhead_ratio": guardrail_percent,
                    "economics_by_intent": economics_by_intent
                }

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
                    "token_economics": token_economics,
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


def _build_report_conditions(
    client_id: str,
    col: str,
    range_type: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    intent: Optional[str] = None,
    resolution_mode: Optional[str] = None,
    escalation_reason: Optional[str] = None,
    search: Optional[str] = None,
):
    where_clauses = ["1=1"]
    params = []

    if client_id != "ALL":
        where_clauses.append(f"e.{col} = %s")
        params.append(client_id)

    if range_type == "today":
        where_clauses.append("e.created_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR")
    elif range_type == "yesterday":
        where_clauses.append("e.created_at >= UTC_TIMESTAMP() - INTERVAL 48 HOUR AND e.created_at < UTC_TIMESTAMP() - INTERVAL 24 HOUR")
    elif range_type == "this_month":
        where_clauses.append("e.created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL DAYOFMONTH(UTC_TIMESTAMP()) - 1 DAY) - INTERVAL HOUR(UTC_TIMESTAMP()) HOUR - INTERVAL MINUTE(UTC_TIMESTAMP()) MINUTE - INTERVAL SECOND(UTC_TIMESTAMP()) SECOND")
    elif range_type == "last_month":
        where_clauses.append("e.created_at >= DATE_SUB(DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 MONTH), INTERVAL DAYOFMONTH(DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 MONTH)) - 1 DAY) - INTERVAL HOUR(UTC_TIMESTAMP()) HOUR - INTERVAL MINUTE(UTC_TIMESTAMP()) MINUTE - INTERVAL SECOND(UTC_TIMESTAMP()) SECOND AND e.created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL DAYOFMONTH(UTC_TIMESTAMP()) - 1 DAY) - INTERVAL HOUR(UTC_TIMESTAMP()) HOUR - INTERVAL MINUTE(UTC_TIMESTAMP()) MINUTE - INTERVAL SECOND(UTC_TIMESTAMP()) SECOND")
    elif range_type == "custom" and start_date and end_date:
        where_clauses.append("e.created_at >= %s AND e.created_at <= %s")
        params.extend([f"{start_date} 00:00:00", f"{end_date} 23:59:59"])

    if intent and intent.lower() != "all":
        where_clauses.append("(LOWER(COALESCE(e.intent, '')) = %s OR LOWER(COALESCE(t.intent, '')) = %s)")
        params.extend([intent.lower(), intent.lower()])

    if escalation_reason and escalation_reason.lower() != "all":
        where_clauses.append("(LOWER(COALESCE(e.escalation_reason, '')) = %s OR LOWER(COALESCE(t.escalation_reason, '')) = %s)")
        params.extend([escalation_reason.lower(), escalation_reason.lower()])

    if resolution_mode and resolution_mode.lower() != "all":
        rm = resolution_mode.lower()
        if rm == "rag":
            where_clauses.append("e.status IN ('sent') AND (e.escalation_reason IS NULL OR e.escalation_reason = '') AND t.ticket_id IS NULL")
        elif rm == "ticket":
            where_clauses.append("(e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL OR (e.escalation_reason IS NOT NULL AND e.escalation_reason != ''))")
        elif rm == "review":
            where_clauses.append("e.status = 'pending_manual_review'")
        elif rm == "failed":
            where_clauses.append("e.status IN ('failed', 'send_failed', 'ticket_creation_failed')")

    if search and search.strip():
        s_term = f"%{search.strip()}%"
        where_clauses.append("(e.subject LIKE %s OR e.from_email LIKE %s OR e.summary LIKE %s OR e.body LIKE %s)")
        params.extend([s_term, s_term, s_term, s_term])

    return " AND ".join(where_clauses), params


@router.get("/reports/ticketing/{client_id}")
def get_ticketing_report_endpoint(
    client_id: str,
    range_type: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    intent: Optional[str] = None,
    resolution_mode: Optional[str] = None,
    escalation_reason: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    page = max(1, page)
    page_size = min(100, max(5, page_size))
    offset = (page - 1) * page_size

    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception:
                    pass

                where_sql, params = _build_report_conditions(
                    client_id, col, range_type, start_date, end_date,
                    intent, resolution_mode, escalation_reason, search
                )

                # Count total matching rows and overall report summary metrics
                cursor.execute(f"""
                    SELECT 
                        COUNT(DISTINCT e.id),
                        SUM(CASE WHEN e.status = 'sent' AND (e.escalation_reason IS NULL OR e.escalation_reason = '') AND t.ticket_id IS NULL THEN 1 ELSE 0 END) as rag_deflected,
                        SUM(CASE WHEN e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL OR (e.escalation_reason IS NOT NULL AND e.escalation_reason != '') THEN 1 ELSE 0 END) as tickets_created,
                        SUM(CASE WHEN e.status = 'pending_manual_review' THEN 1 ELSE 0 END) as manual_reviews,
                        AVG(e.score) as avg_score
                    FROM email_logs e
                    LEFT JOIN ticket_record t ON (t.mail_id = e.message_id OR (t.thread_id = e.thread_id AND e.thread_id IS NOT NULL))
                    WHERE {where_sql}
                """, tuple(params))
                agg_row = cursor.fetchone()
                total_rows = int(agg_row[0] or 0) if agg_row else 0
                rag_deflected = int(agg_row[1] or 0) if agg_row else 0
                tickets_created = int(agg_row[2] or 0) if agg_row else 0
                manual_reviews = int(agg_row[3] or 0) if agg_row else 0
                avg_score = round(float(agg_row[4] or 0.0), 1) if agg_row and agg_row[4] is not None else 0.0

                # Query paginated rows with pre-aggregated cost and turn counts to avoid correlated subquery overhead
                paginated_params = list(params)
                paginated_params.extend([page_size, offset])

                cursor.execute(f"""
                    SELECT 
                        e.id,
                        COALESCE(e.thread_id, CONCAT('th_', e.id)) as thread_id,
                        e.created_at,
                        e.from_email,
                        e.subject,
                        COALESCE(NULLIF(e.summary, ''), SUBSTRING(COALESCE(e.body, ''), 1, 150)) as summary_of_issue,
                        COALESCE(NULLIF(e.intent, ''), NULLIF(t.intent, ''), 'general_inquiry') as technical_intent,
                        CASE 
                            WHEN e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL THEN 'Ticket Escalated'
                            WHEN e.status = 'pending_manual_review' THEN 'Manual Review'
                            WHEN e.status = 'sent' THEN 'RAG Deflected'
                            WHEN e.status IN ('failed', 'send_failed', 'ticket_creation_failed') THEN 'Failed'
                            ELSE 'Processing'
                        END as res_mode,
                        CASE 
                            WHEN e.status = 'sent' THEN 'Resolved'
                            WHEN e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL THEN 'Escalated'
                            WHEN e.status = 'pending_manual_review' THEN 'Under Review'
                            WHEN e.status IN ('failed', 'send_failed', 'ticket_creation_failed') THEN 'Failed'
                            ELSE 'Open'
                        END as status_label,
                        COALESCE(NULLIF(e.escalation_reason, ''), NULLIF(t.escalation_reason, ''), 'None') as escalation_reason,
                        COALESCE(e.score, 80) as score,
                        COALESCE(e.sentiment, 'Neutral') as sentiment,
                        COALESCE(tc.turn_count, 1) as turns,
                        COALESCE(lc.thread_cost, 0.0) as estimated_cost
                    FROM email_logs e
                    LEFT JOIN ticket_record t ON (t.mail_id = e.message_id OR (t.thread_id = e.thread_id AND e.thread_id IS NOT NULL))
                    LEFT JOIN (
                        SELECT thread_id, COUNT(*) as turn_count 
                        FROM email_logs 
                        WHERE thread_id IS NOT NULL 
                        GROUP BY thread_id
                    ) tc ON e.thread_id = tc.thread_id
                    LEFT JOIN (
                        SELECT l.thread_id, SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as thread_cost
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.thread_id IS NOT NULL
                        GROUP BY l.thread_id
                    ) lc ON e.thread_id = lc.thread_id
                    WHERE {where_sql}
                    ORDER BY e.created_at DESC
                    LIMIT %s OFFSET %s
                """, tuple(paginated_params))
                rows = cursor.fetchall()

                records = []
                for r in rows:
                    r_id, th_id, dt, from_em, subj, summary_txt, intent_str, res_mode, st_label, esc_reason, score_val, sent_val, turns_cnt, cost_val = r
                    cost_flt = round(float(cost_val or 0.0), 5)
                    sent_clean = (sent_val or "Neutral").strip().capitalize()

                    # Derive sentiment trajectory shift and sentiment score (-1.0 to 1.0)
                    if sent_clean.lower() in ('positive', 'delighted'):
                        sentiment_numeric = 1.0
                    elif sent_clean.lower() in ('negative', 'frustrated', 'urgent'):
                        sentiment_numeric = -1.0
                    else:
                        sentiment_numeric = 0.0

                    if res_mode == 'RAG Deflected':
                        shift = f"{sent_clean} → Resolved"
                    elif res_mode == 'Ticket Escalated':
                        shift = f"{sent_clean} → Ticket Created"
                    elif res_mode == 'Manual Review':
                        shift = f"{sent_clean} → Human Review"
                    else:
                        shift = f"{sent_clean} → In Progress"

                    records.append({
                        "id": r_id,
                        "thread_id": th_id,
                        "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
                        "customer_email": from_em or "",
                        "subject": subj or "No Subject",
                        "summary_of_issue": summary_txt or "",
                        "technical_intent": intent_str or "general_inquiry",
                        "resolution_mode": res_mode,
                        "status": st_label,
                        "escalation_reason": esc_reason if esc_reason != 'None' else None,
                        "total_turns": max(1, int(turns_cnt or 1)),
                        "confidence_score": int(score_val or 80),
                        "sentiment": sent_clean,
                        "sentiment_shift": shift,
                        "sentiment_score": sentiment_numeric,
                        "estimated_cost": cost_flt
                    })

                total_pages = (total_rows + page_size - 1) // page_size if total_rows > 0 else 1

                return {
                    "status": "success",
                    "client_id": client_id,
                    "summary": {
                        "total_records": total_rows,
                        "rag_deflected": rag_deflected,
                        "tickets_created": tickets_created,
                        "manual_reviews": manual_reviews,
                        "deflection_rate": round((rag_deflected / max(1, rag_deflected + tickets_created)) * 100, 1) if (rag_deflected + tickets_created) > 0 else 0.0,
                        "avg_confidence": avg_score
                    },
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "total_rows": total_rows,
                        "total_pages": total_pages
                    },
                    "records": records
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch ticketing report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reports/export/{client_id}")
def export_ticketing_report_endpoint(
    client_id: str,
    range_type: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    intent: Optional[str] = None,
    resolution_mode: Optional[str] = None,
    escalation_reason: Optional[str] = None,
    search: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)

    def iter_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "Thread ID", "Timestamp (UTC)", "Customer Email", "Subject",
            "Summary of Issue", "Technical Intent", "Resolution Mode", "Status",
            "Escalation Reason", "Total Turns", "Confidence Score", "Sentiment",
            "Sentiment Trajectory", "Sentiment Score (-1 to 1)", "Estimated Cost ($)"
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception:
                    pass

                where_sql, params = _build_report_conditions(
                    client_id, col, range_type, start_date, end_date,
                    intent, resolution_mode, escalation_reason, search
                )

                cursor.execute(f"""
                    SELECT 
                        e.id,
                        COALESCE(e.thread_id, CONCAT('th_', e.id)) as thread_id,
                        e.created_at,
                        e.from_email,
                        e.subject,
                        COALESCE(NULLIF(e.summary, ''), SUBSTRING(COALESCE(e.body, ''), 1, 150)) as summary_of_issue,
                        COALESCE(NULLIF(e.intent, ''), NULLIF(t.intent, ''), 'general_inquiry') as technical_intent,
                        CASE 
                            WHEN e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL THEN 'Ticket Escalated'
                            WHEN e.status = 'pending_manual_review' THEN 'Manual Review'
                            WHEN e.status = 'sent' THEN 'RAG Deflected'
                            WHEN e.status IN ('failed', 'send_failed', 'ticket_creation_failed') THEN 'Failed'
                            ELSE 'Processing'
                        END as res_mode,
                        CASE 
                            WHEN e.status = 'sent' THEN 'Resolved'
                            WHEN e.status IN ('ticket_created_and_sent', 'ticket_created_send_failed') OR t.ticket_id IS NOT NULL THEN 'Escalated'
                            WHEN e.status = 'pending_manual_review' THEN 'Under Review'
                            WHEN e.status IN ('failed', 'send_failed', 'ticket_creation_failed') THEN 'Failed'
                            ELSE 'Open'
                        END as status_label,
                        COALESCE(NULLIF(e.escalation_reason, ''), NULLIF(t.escalation_reason, ''), 'None') as escalation_reason,
                        COALESCE(e.score, 80) as score,
                        COALESCE(e.sentiment, 'Neutral') as sentiment,
                        COALESCE(tc.turn_count, 1) as turns,
                        COALESCE(lc.thread_cost, 0.0) as estimated_cost
                    FROM email_logs e
                    LEFT JOIN ticket_record t ON (t.mail_id = e.message_id OR (t.thread_id = e.thread_id AND e.thread_id IS NOT NULL))
                    LEFT JOIN (
                        SELECT thread_id, COUNT(*) as turn_count 
                        FROM email_logs 
                        WHERE thread_id IS NOT NULL 
                        GROUP BY thread_id
                    ) tc ON e.thread_id = tc.thread_id
                    LEFT JOIN (
                        SELECT l.thread_id, SUM(l.cost * COALESCE(ea.cost_multiplier, 1.0)) as thread_cost
                        FROM llm_logs l
                        LEFT JOIN email_accounts ea ON l.client_id = ea.client_id
                        WHERE l.thread_id IS NOT NULL
                        GROUP BY l.thread_id
                    ) lc ON e.thread_id = lc.thread_id
                    WHERE {where_sql}
                    ORDER BY e.created_at DESC
                """, tuple(params))

                while True:
                    batch = cursor.fetchmany(100)
                    if not batch:
                        break
                    for r in batch:
                        r_id, th_id, dt, from_em, subj, summary_txt, intent_str, res_mode, st_label, esc_reason, score_val, sent_val, turns_cnt, cost_val = r
                        cost_flt = round(float(cost_val or 0.0), 5)
                        sent_clean = (sent_val or "Neutral").strip().capitalize()

                        if sent_clean.lower() in ('positive', 'delighted'):
                            sentiment_score = 1.0
                        elif sent_clean.lower() in ('negative', 'frustrated', 'urgent'):
                            sentiment_score = -1.0
                        else:
                            sentiment_score = 0.0

                        if res_mode == 'RAG Deflected':
                            shift = f"{sent_clean} -> Resolved"
                        elif res_mode == 'Ticket Escalated':
                            shift = f"{sent_clean} -> Ticket Created"
                        elif res_mode == 'Manual Review':
                            shift = f"{sent_clean} -> Human Review"
                        else:
                            shift = f"{sent_clean} -> In Progress"

                        writer.writerow([
                            r_id,
                            th_id,
                            dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
                            from_em or "",
                            subj or "",
                            summary_txt or "",
                            intent_str,
                            res_mode,
                            st_label,
                            esc_reason if esc_reason != 'None' else "None",
                            max(1, int(turns_cnt or 1)),
                            int(score_val or 80),
                            sent_clean,
                            shift,
                            sentiment_score,
                            cost_flt
                        ])
                        yield output.getvalue()
                        output.seek(0)
                        output.truncate(0)

    filename = f"resolution_report_{client_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

