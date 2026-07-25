"""
app/integrations/gmail.py

Sends transactional notification emails via Gmail SMTP.
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
import structlog
from app.config import get_settings
from app.orchestrator.verdict import generate_verdict_text
from app.services.transaction_store import transaction_store

log = structlog.get_logger(__name__)

def send_applicant_email(to_address: str, subject: str, body: str) -> bool:
    """
    Sends a plain-text email via Gmail SMTP. Returns True/False, never
    raises to the caller — mirror the Slack executor's failure handling.
    """
    settings = get_settings()

    if getattr(settings, "use_mock_gmail", False):
        from app.integrations.mock_services import mock_gmail_send
        log.info("gmail.mock.send", to=to_address, subject=subject)
        mock_gmail_send(to_address, subject, body)
        return True

    sender = settings.gmail_sender_address
    password = getattr(settings, "gmail_app_password", None)

    if not sender or not password:
        log.warning("gmail.disabled", reason="Missing GMAIL_SENDER_ADDRESS or GMAIL_APP_PASSWORD")
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_address

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10.0) as server:
            server.login(sender, password)
            server.send_message(msg)
        log.info("gmail.sent", to=to_address, subject=subject)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        log.warning("gmail.auth_failed", error=str(exc))
        return False
    except Exception as exc:
        log.warning("gmail.failed", error=str(exc))
        return False

def send_verdict_email(ctx) -> None:
    tool_key = f"gmail:verdict:{ctx.transaction_id}"
    if transaction_store.has_executed_tool(ctx.transaction_id, tool_key):
        return

    verdict_text = generate_verdict_text(ctx)
    subject = f"Update on your {ctx.transaction_type.value} application \u2014 {ctx.transaction_id}"
    
    body = (
        f"Dear {ctx.customer_name},\n\n"
        f"Transaction ID: {ctx.transaction_id}\n"
        f"Requested Amount: \u20b9{ctx.requested_amount}\n\n"
        f"{verdict_text}\n"
    )
    if ctx.decision_type == "MANAGER_DECISION":
        body += "\nA manager has reviewed this application personally.\n"

    # Enforce test inbox rule
    safe_to = f"test+{ctx.customer_id}@mockbank.example.com"

    success = send_applicant_email(safe_to, subject, body)
    if success:
        transaction_store.mark_tool_executed(ctx.transaction_id, tool_key)

def send_credit_confirmation_email(ctx) -> None:
    tool_key = f"gmail:credit_confirmed:{ctx.transaction_id}"
    if transaction_store.has_executed_tool(ctx.transaction_id, tool_key):
        return

    verdict_text = generate_verdict_text(ctx)
    subject = f"Funds disbursed \u2014 {ctx.transaction_id}"

    credited_by = ctx.credited_by or "System"
    credited_at = ctx.credited_at.isoformat() if ctx.credited_at else "Now"

    body = (
        f"Dear {ctx.customer_name},\n\n"
        f"Your funds have been disbursed successfully.\n\n"
        f"Transaction ID: {ctx.transaction_id}\n"
        f"Amount Credited: \u20b9{ctx.requested_amount}\n"
        f"Credited By: {credited_by}\n"
        f"Credited At: {credited_at}\n\n"
        f"Reasoning:\n{verdict_text}\n"
    )

    safe_to = f"test+{ctx.customer_id}@mockbank.example.com"
    success = send_applicant_email(safe_to, subject, body)
    if success:
        transaction_store.mark_tool_executed(ctx.transaction_id, tool_key)
