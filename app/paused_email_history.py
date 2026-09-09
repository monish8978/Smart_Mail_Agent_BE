

def ensure_paused_email_history_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paused_email_history (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            client_id      VARCHAR(50) NOT NULL,
            from_email     VARCHAR(255) NOT NULL,
            subject        TEXT,
            body           TEXT,
            status         ENUM('pending_review', 'ignored', 'replied') DEFAULT 'pending_review',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_client_status (client_id, status),
            INDEX idx_client_email (client_id, from_email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)