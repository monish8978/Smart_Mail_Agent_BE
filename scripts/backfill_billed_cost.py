from app.db import get_db_ctx

def run_backfill():
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE llm_logs l
                LEFT JOIN email_accounts ea ON ea.client_id = l.client_id
                SET l.billed_cost = ROUND(l.cost * COALESCE(ea.cost_multiplier, 1.0), 6)
                WHERE l.billed_cost IS NULL
            """)
            affected = cursor.rowcount
            db.commit()
            print(f"Successfully backfilled {affected} rows.")
            
            cursor.execute("SELECT COUNT(*), COUNT(billed_cost) FROM llm_logs")
            total, billed = cursor.fetchone()
            print(f"Total rows: {total}, Billed rows populated: {billed}")

if __name__ == "__main__":
    run_backfill()
