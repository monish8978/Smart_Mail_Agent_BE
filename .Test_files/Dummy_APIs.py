"""
Dummy Ticket API Server
=======================
Simulates a ticketing system for development/testing.

Endpoints:
  POST  /create-ticket?data=<base64>        — Create a new ticket
  GET   /get-ticket?postData=<base64>       — Fetch ticket details by docket_no
  GET   /tickets                            — List all tickets
  PATCH /tickets/<ticket_id>/status         — Update ticket status

Run:
  pip install flask
  python dummy_ticket_server.py

DB: dummy_tickets.db (SQLite, auto-created on first run)
"""

import base64
import json
import logging
import random
import sqlite3
from datetime import datetime
from contextlib import contextmanager

from flask import Flask, request, jsonify

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
DB_PATH = "dummy_tickets.db"
PORT    = 9000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# ──────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id         TEXT PRIMARY KEY,
                client_id         TEXT,
                mail_id           TEXT,
                subject           TEXT,
                body              TEXT,
                status            TEXT DEFAULT 'Open',
                priority_name     TEXT DEFAULT 'Medium',
                ticket_type       TEXT DEFAULT 'Support',
                problem_reported  TEXT,
                agent_remarks     TEXT DEFAULT '',
                disposition_name  TEXT DEFAULT '',
                sub_disposition   TEXT DEFAULT '',
                assigned_dept     TEXT DEFAULT 'Support',
                assigned_user     TEXT DEFAULT 'Auto-Assigned',
                person_name       TEXT DEFAULT '',
                person_mail       TEXT DEFAULT '',
                mobile_no         TEXT DEFAULT '',
                created_at        TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    logger.info("✅ DB initialised — dummy_tickets.db")


def generate_ticket_id() -> str:
    date_part   = datetime.now().strftime("%y%m%d")
    random_part = str(random.randint(0, 99999)).zfill(5)
    return f"T-{date_part}-{random_part}"


def decode_payload(encoded: str) -> dict:
    decoded = base64.b64decode(encoded).decode("utf-8")
    return json.loads(decoded)


def infer_priority(subject: str, body: str) -> str:
    text = (subject + " " + body).lower()
    if any(w in text for w in ["urgent", "critical", "asap", "immediately", "emergency"]):
        return "High"
    if any(w in text for w in ["refund", "cancel", "broken", "failed", "error"]):
        return "Medium"
    return "Low"


def infer_ticket_type(subject: str, body: str) -> str:
    text = (subject + " " + body).lower()
    if any(w in text for w in ["order", "delivery", "shipping", "track"]):
        return "Order"
    if any(w in text for w in ["refund", "money", "payment", "charge"]):
        return "Billing"
    if any(w in text for w in ["account", "login", "password", "access"]):
        return "Account"
    return "Support"


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/create-ticket", methods=["GET", "POST"])
def create_ticket():
    """
    Receives base64-encoded JSON via ?data=
    Expected payload keys (all optional except client_id):
        client_id, mail_id, subject, body, status,
        person_name, person_mail, mobile_no, priority_name
    """
    encoded = request.args.get("data") or (request.json or {}).get("data")
    if not encoded:
        return jsonify({"Status": "Failure", "Message": "Missing 'data' parameter"}), 400

    try:
        payload = decode_payload(encoded)
    except Exception as e:
        logger.error(f"❌ Failed to decode payload: {e}")
        return jsonify({"Status": "Failure", "Message": "Invalid base64/JSON payload"}), 400

    ticket_id        = generate_ticket_id()
    client_id        = payload.get("client_id", "")
    mail_id          = payload.get("mail_id", "")
    subject          = payload.get("subject", "No Subject")
    body             = payload.get("body", "")
    person_name      = payload.get("person_name", "")
    person_mail      = payload.get("person_mail", mail_id)
    mobile_no        = payload.get("mobile_no", "")
    priority_name    = payload.get("priority_name") or infer_priority(subject, body)
    ticket_type      = payload.get("ticket_type")  or infer_ticket_type(subject, body)
    problem_reported = subject

    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO tickets (
                    ticket_id, client_id, mail_id, subject, body,
                    status, priority_name, ticket_type, problem_reported,
                    person_name, person_mail, mobile_no
                ) VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?, ?, ?)
            """, (
                ticket_id, client_id, mail_id, subject, body,
                priority_name, ticket_type, problem_reported,
                person_name, person_mail, mobile_no
            ))

        logger.info(f"✅ Ticket created: {ticket_id} for client_id={client_id}")
        return jsonify({
            "Status":      "Success",
            "Message":     f"Ticket {ticket_id} created successfully",
            "Refrence_No": ticket_id
        })

    except Exception as e:
        logger.error(f"❌ DB insert failed: {e}")
        return jsonify({"Status": "Failure", "Message": str(e)}), 500


@app.route("/get-ticket", methods=["GET"])
def get_ticket():
    """
    Receives base64-encoded JSON via ?postData=
    Expected payload: {"filter": {"docket_no": "T-XXXXXX-XXXXX"}}
    """
    encoded = request.args.get("postData")
    if not encoded:
        return jsonify({"Failure": "Missing 'postData' parameter"}), 400

    try:
        payload = decode_payload(encoded)
    except Exception as e:
        logger.error(f"❌ Failed to decode postData: {e}")
        return jsonify({"Failure": "Invalid base64/JSON payload"}), 400

    ticket_id = payload.get("filter", {}).get("docket_no")
    if not ticket_id:
        return jsonify({"Failure": "Missing docket_no in filter"}), 400

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()

        if not row:
            return jsonify({"Failure": f"Ticket {ticket_id} not found"}), 404

        return jsonify({
            "Success": {
                ticket_id: {
                    "docket_no":              row["ticket_id"],
                    "ticket_status":          row["status"],
                    "priority_name":          row["priority_name"],
                    "ticket_type":            row["ticket_type"],
                    "problem_reported":       row["problem_reported"],
                    "agent_remarks":          row["agent_remarks"],
                    "disposition_name":       row["disposition_name"],
                    "sub_disposition_name":   row["sub_disposition"],
                    "assigned_to_dept_name":  row["assigned_dept"],
                    "assigned_to_user_name":  row["assigned_user"],
                    "created_at":             row["created_at"],
                    "person": {
                        "person_name":  row["person_name"],
                        "person_mail":  row["person_mail"],
                        "mobile_no":    row["mobile_no"]
                    }
                }
            }
        })

    except Exception as e:
        logger.error(f"❌ DB fetch failed: {e}")
        return jsonify({"Failure": str(e)}), 500


@app.route("/tickets", methods=["GET"])
def list_tickets():
    """List all tickets — useful for debugging."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC"
            ).fetchall()

        tickets = [dict(r) for r in rows]
        return jsonify({"total": len(tickets), "tickets": tickets})

    except Exception as e:
        logger.error(f"❌ List failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/tickets/<ticket_id>/status", methods=["PATCH"])
def update_ticket_status(ticket_id: str):
    """
    Update ticket status manually.
    Body: {"status": "Resolved"}
    Valid: Open, In Progress, Resolved, Closed
    """
    VALID_STATUSES = {"Open", "In Progress", "Resolved", "Closed"}

    body = request.get_json()
    if not body or "status" not in body:
        return jsonify({"error": "Missing 'status' in request body"}), 400

    new_status = body["status"]
    if new_status not in VALID_STATUSES:
        return jsonify({
            "error": f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}"
        }), 400

    try:
        with get_db() as conn:
            result = conn.execute(
                "UPDATE tickets SET status = ? WHERE ticket_id = ?",
                (new_status, ticket_id)
            )
            if result.rowcount == 0:
                return jsonify({"error": f"Ticket {ticket_id} not found"}), 404

        logger.info(f"✅ Ticket {ticket_id} status updated to '{new_status}'")
        return jsonify({
            "Status":    "Success",
            "ticket_id": ticket_id,
            "status":    new_status
        })

    except Exception as e:
        logger.error(f"❌ Status update failed: {e}")
        return jsonify({"error": str(e)}), 500





@app.route("/v2/create-ticket", methods=["POST"])
def create_ticket_v2():
    """Plain JSON body version, for the new connector_configs executor."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"Status": "Failure", "Message": "Missing or invalid JSON body"}), 400

    ticket_id = generate_ticket_id()
    client_id = payload.get("client_id", "")
    mail_id = payload.get("mail_id", payload.get("from_email", ""))
    subject = payload.get("subject", "No Subject")
    body = payload.get("body", "")
    person_name = payload.get("person_name", "")
    priority_name = payload.get("priority_name") or infer_priority(subject, body)
    ticket_type = payload.get("ticket_type") or infer_ticket_type(subject, body)

    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO tickets (
                    ticket_id, client_id, mail_id, subject, body,
                    status, priority_name, ticket_type, problem_reported, person_name
                ) VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?)
            """, (ticket_id, client_id, mail_id, subject, body, priority_name, ticket_type, subject, person_name))

        logger.info(f"✅ [v2] Ticket created: {ticket_id} for client_id={client_id}")
        return jsonify({
            "status": "success",
            "message": f"Ticket {ticket_id} created successfully",
            "reference_no": ticket_id
        })
    except Exception as e:
        logger.error(f"❌ [v2] DB insert failed: {e}")
        return jsonify({"status": "failure", "message": str(e)}), 500


@app.route("/v2/get-ticket", methods=["GET"])
def get_ticket_v2():
    """Plain query-param version, for the new connector_configs executor."""
    ticket_id = request.args.get("docket_no")
    if not ticket_id:
        return jsonify({"status": "failure", "message": "Missing docket_no query param"}), 400

    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()

        if not row:
            return jsonify({"status": "failure", "message": f"Ticket {ticket_id} not found"}), 404

        return jsonify({
            "status": "success",
            "docket_no": row["ticket_id"],
            "ticket_status": row["status"],
            "priority_name": row["priority_name"],
            "problem_reported": row["problem_reported"],
            "person_name": row["person_name"]
        })
    except Exception as e:
        logger.error(f"❌ [v2] DB fetch failed: {e}")
        return jsonify({"status": "failure", "message": str(e)}), 500





# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    logger.info(f"🚀 Dummy Ticket Server running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=True)