import logging
import smtplib

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from worker.credential_service import get_email_credentials

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def send_email(client_id, to_email, subject, body, in_reply_to=None):
    """Send an email using SMTP and credentials from the credential service."""

    try:
        # Retrieve email credentials
        if client_id == "registration":
            import os
            EMAIL_USER = os.getenv("REGISTRATION_EMAIL", "").strip()
            EMAIL_PASS = os.getenv("REGISTRATION_EMAIL_PASS", "").strip('"\'')
        else:
            EMAIL_USER, EMAIL_PASS = get_email_credentials(client_id)

        if not EMAIL_USER or not EMAIL_PASS:
            logger.error("❌ Email credentials not found for client %s, aborting send_email", client_id)
            return False

        logger.info("📤 Preparing to send email to %s using client %s", to_email, client_id)

        # Create MIME message with UTF-8 support
        message = MIMEMultipart()
        message["From"] = EMAIL_USER
        message["To"] = to_email
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to

        # Attach body with UTF-8 encoding
        message.attach(MIMEText(body, "plain", "utf-8"))

        # Connect SMTP
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()

        logger.debug("🔐 Started TLS session with SMTP server")

        # Login
        server.login(EMAIL_USER, EMAIL_PASS)

        logger.debug("✅ SMTP login successful for %s", EMAIL_USER)

        # Send email
        server.sendmail(
            EMAIL_USER,
            to_email,
            message.as_string()
        )

        logger.info("✅ Email sent successfully to %s", to_email)

        server.quit()

        return True

    except Exception as exc:
        logger.error(
            "❌ Failed to send email to %s: %s",
            to_email,
            exc
        )
        return False