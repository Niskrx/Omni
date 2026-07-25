"""
Slack integration for posting webhook notifications.
"""
from __future__ import annotations

import httpx
import structlog
from app.config import get_settings
from app.orchestrator.verdict import generate_verdict_text

log = structlog.get_logger(__name__)


def post_slack_message(channel_webhook_url: str, text: str, blocks: list | None = None) -> bool:
    """
    Posts to the Slack Incoming Webhook. Returns True/False on success, never
    raises — a Slack outage must not break the transaction pipeline. Log
    failures clearly but swallow them at the call site with a warning, not
    a silent pass.
    """
    settings = get_settings()

    if settings.use_mock_slack:
        from app.integrations.mock_services import mock_slack_post_message
        log.info("slack.mock.send", text=text[:50])
        mock_slack_post_message(channel_webhook_url, text, blocks)
        return True

    if not channel_webhook_url:
        log.warning("slack.disabled", reason="No webhook URL provided")
        return False

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        response = httpx.post(channel_webhook_url, json=payload, timeout=5.0)
        response.raise_for_status()
        log.info("slack.posted", status_code=response.status_code)
        return True
    except httpx.HTTPError as exc:
        log.warning("slack.failed", error=str(exc))
        return False


def send_review_queue_alert(ctx, summary) -> None:
    from app.services.transaction_store import transaction_store

    tool_key = f"slack:review_queue:{ctx.transaction_id}"
    if transaction_store.has_executed_tool(ctx.transaction_id, tool_key):
        return

    settings = get_settings()
    webhook_url = settings.slack_webhook_url

    dashboard_url = "http://localhost:8000"
    summary_text = "\n".join(summary.bullets)

    text = (
        f"🔴 Manager review needed — {ctx.transaction_id}\n"
        f"Applicant: {ctx.customer_name} | Amount: ₹{ctx.requested_amount} | Type: {ctx.transaction_type.value}\n"
        f"{summary_text}\n"
        f"Review at: {dashboard_url}/status/{ctx.transaction_id}"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔴 Manager review needed — {ctx.transaction_id}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Applicant:* {ctx.customer_name} | *Amount:* ₹{ctx.requested_amount} | *Type:* {ctx.transaction_type.value}\n\n"
                    f"{summary_text}\n\n"
                    f"<{dashboard_url}/status/{ctx.transaction_id}|*Review on Dashboard*>"
                )
            }
        }
    ]

    post_slack_message(webhook_url, text, blocks)
    transaction_store.mark_tool_executed(ctx.transaction_id, tool_key)


def send_credit_confirmed_alert(ctx) -> None:
    from app.services.transaction_store import transaction_store

    tool_key = f"slack:credit_confirmed:{ctx.transaction_id}"
    if transaction_store.has_executed_tool(ctx.transaction_id, tool_key):
        return

    settings = get_settings()
    webhook_url = settings.slack_webhook_url

    verdict_text = generate_verdict_text(ctx)
    dashboard_url = "http://localhost:8000"

    text = (
        f"🟢 Credit Confirmed — {ctx.transaction_id}\n"
        f"Applicant: {ctx.customer_name} | Amount: ₹{ctx.requested_amount} | Type: {ctx.transaction_type.value}\n"
        f"Verdict: {verdict_text}\n"
        f"View details: {dashboard_url}/status/{ctx.transaction_id}"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🟢 Credit Confirmed — {ctx.transaction_id}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Applicant:* {ctx.customer_name} | *Amount:* ₹{ctx.requested_amount} | *Type:* {ctx.transaction_type.value}\n"
                    f"*Verdict:* {verdict_text}\n\n"
                    f"<{dashboard_url}/status/{ctx.transaction_id}|*View details*>"
                )
            }
        }
    ]

    post_slack_message(webhook_url, text, blocks)
    transaction_store.mark_tool_executed(ctx.transaction_id, tool_key)
