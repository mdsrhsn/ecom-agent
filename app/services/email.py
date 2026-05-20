"""SMTP email sender."""
import smtplib
from email.message import EmailMessage
from app.config import settings


def send_email(to, subject: str, body: str, html: str = None) -> bool:
    if not (settings.SMTP_USER and settings.SMTP_PASS):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_USER
    recipients = [to] if isinstance(to, str) else to
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        return False
