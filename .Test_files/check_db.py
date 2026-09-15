import os
import pymysql

# Set env variables so get_db works
os.environ["DB_HOST"] = "172.16.3.215"
os.environ["DB_USER"] = "root"
os.environ["DB_PASS"] = "sqladmin"
os.environ["DB_NAME"] = "ai_mail_bot"

conn = pymysql.connect(
    host=os.environ["DB_HOST"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASS"],
    database=os.environ["DB_NAME"]
)
cursor = conn.cursor()

print("--- llm_logs count ---")
try:
    cursor.execute("SELECT COUNT(*), client_id FROM llm_logs GROUP BY client_id")
    print(cursor.fetchall())
except Exception as e:
    print(f"Error checking llm_logs: {e}")

print("\n--- llm_logs samples ---")
try:
    cursor.execute("SELECT * FROM llm_logs LIMIT 5")
    print(cursor.fetchall())
except Exception as e:
    print(e)

print("\n--- email_logs count ---")
try:
    cursor.execute("SELECT COUNT(*), client_id FROM email_logs GROUP BY client_id")
    print(cursor.fetchall())
except Exception as e:
    print(e)

print("\n--- email_logs samples ---")
try:
    cursor.execute("SELECT id, client_id, from_email, status FROM email_logs LIMIT 5")
    print(cursor.fetchall())
except Exception as e:
    print(e)

conn.close()
