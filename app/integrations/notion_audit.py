"""
app/integrations/notion_audit.py

Writes compressed executive summaries to the Notion Audit & Activity Feed.

Populates all required fields per Policy Book Section III, Clause 1:
  - TransactionID
  - AgentsInvolved
  - Route
  - Outcome
  - Timestamp
  - PolicyVersion

Plus a Summary field containing the compressed bullet list.

In mock mode (USE_MOCK_NOTION=true) no real API calls are made.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import get_settings
from app.models.transaction import TransactionContext

log = structlog.get_logger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionAuditError(Exception):
    """Raised when the Notion Audit Feed write fails."""


class NotionAuditClient:
    """Writes audit entries to the Notion Audit & Activity Feed database."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def create_audit_entry(self, ctx: TransactionContext, summary) -> dict:
        """
        Write one row to the Audit & Activity Feed.

        Args:
            ctx: The original TransactionContext.
            summary: A CompressedSummary object from orchestrator/history.py.

        Returns:
            Notion API response (or mock equivalent).

        Raises:
            NotionAuditError: on failure.
        """
        from app.orchestrator.history import CompressedSummary  # local to avoid circular
        assert isinstance(summary, CompressedSummary)

        bullet_text = "\n".join(summary.bullets)
        agents_text = ", ".join(summary.agents_involved) or "None"
        timestamp = summary.compressed_at.isoformat()

        if self._settings.use_mock_notion:
            log.info(
                "notion_audit.mock.create_entry",
                transaction_id=str(ctx.transaction_id),
                outcome=summary.outcome,
                policy_version=summary.policy_version,
                agents=agents_text,
            )
            return {
                "object": "page",
                "id": f"mock-audit-{str(ctx.transaction_id)[:8]}",
                "properties": {
                    "TransactionID": str(ctx.transaction_id),
                    "AgentsInvolved": agents_text,
                    "Route": summary.route,
                    "Outcome": summary.outcome,
                    "Timestamp": timestamp,
                    "PolicyVersion": summary.policy_version,
                    "Summary": bullet_text,
                },
                "_mock": True,
            }

        # ── Live Notion API call ──────────────────────────────────────────────
        database_id = self._settings.notion_audit_feed_id
        payload = {
            "parent": {"database_id": database_id},
            "properties": {
                # TransactionID — Title property
                "TransactionID": {
                    "title": [{"text": {"content": str(ctx.transaction_id)}}]
                },
                # AgentsInvolved — multi_select
                "AgentsInvolved": {
                    "multi_select": [{"name": agent} for agent in summary.agents_involved]
                },
                # Route — select
                "Route": {
                    "select": {"name": summary.route}
                },
                # Outcome — select
                "Outcome": {
                    "select": {"name": summary.outcome}
                },
                # PolicyVersion — rich text
                "PolicyVersion": {
                    "rich_text": [{"text": {"content": summary.policy_version}}]
                },
                # Summary — rich text (bullet list, truncated to 2000 chars)
                "Summary": {
                    "rich_text": [{"text": {"content": bullet_text[:2000]}}]
                },
                # Timestamp — date
                "Timestamp": {
                    "date": {"start": timestamp}
                },
            },
        }

        try:
            headers = {
                "Authorization": f"Bearer {self._settings.notion_token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=15) as client:
                resp = client.post(f"{_NOTION_API}/pages", json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.error("notion_audit.create_entry_failed", error=str(exc))
            raise NotionAuditError(f"Failed to write audit entry: {exc}") from exc

    def create_approval_desk_entry(self, ctx: TransactionContext, summary, eligibility=None, triggers=None) -> dict:
        """Create a card on the Manager Approval Desk for escalated transactions."""
        from app.orchestrator.history import CompressedSummary
        from app.models.transaction import EligibilityClassification
        assert isinstance(summary, CompressedSummary)

        if self._settings.use_mock_notion:
            log.info("notion_audit.mock.create_approval_desk_entry", transaction_id=str(ctx.transaction_id))
            return {"_mock": True}

        database_id = getattr(self._settings, "notion_approval_desk_id", "mock-desk-db")
        trigger_display = "Subsidy Loan" if "loan" in ctx.transaction_type.value.lower() else "Emergency Payout"
        
        review_type = "Release Confirmation" if eligibility == EligibilityClassification.ELIGIBLE_AUTO else "Judgment Required"
        
        # Build pure Python conflict summary
        def build_conflict_summary(triggers: list[str] | None, txn_id) -> str:
            lines = []
            if triggers:
                for t in triggers:
                    if "PR > 0.30" in t:
                        try:
                            pr_pct = (float(ctx.requested_amount) / float(ctx.annual_declared_income)) * 100
                            lines.append(f"• Flagged under Section I, Clause 6 — requested amount is {pr_pct:.1f}% of declared annual income.")
                        except (ValueError, ZeroDivisionError):
                            lines.append("• Flagged under Section I, Clause 6 — Proportionality check failed.")
                    elif "Frozen fund" in t:
                        lines.append("• Flagged under Section I, Clause 5 — Target fund is frozen.")
                    else:
                        lines.append(f"• Flagged: {t}")
            else:
                lines.append("No conflict detected. Agents reached consensus.")
                
            # Add negotiation rounds
            from app.services.transaction_store import transaction_store
            from app.orchestrator.history import transcript_store
            
            raw_transcript = transcript_store.get_raw_transcript(txn_id)
            if raw_transcript:
                lines.append("\nNegotiation History:")
                # Determine rounds by looking at the sequence
                # A round usually starts with a proposal from one agent, and then another responds.
                round_num = 1
                for i in range(0, len(raw_transcript) - 1, 2):
                    p1 = raw_transcript[i]
                    p2 = raw_transcript[i+1] if i+1 < len(raw_transcript) else None
                    if p2:
                        lines.append(f"Round {round_num}: {p1.originated_by} proposed ₹{p1.proposed_amount} -> {p2.originated_by} responded ({p2.status})")
                    else:
                        lines.append(f"Round {round_num}: {p1.originated_by} proposed ₹{p1.proposed_amount}")
                    round_num += 1
                    
            return "\n".join(lines)
            
        conflict_summary_text = build_conflict_summary(triggers, ctx.transaction_id)
        
        payload = {
            "parent": {"database_id": database_id},
            "properties": {
                "TransactionID": {"title": [{"text": {"content": str(ctx.transaction_id)}}]},
                "Amount": {"number": float(ctx.requested_amount)},
                "Subject": {"rich_text": [{"text": {"content": f"{trigger_display} escalated for review."}}]},
                "Status": {"select": {"name": "Awaiting Action"}},
                "ReviewType": {"select": {"name": review_type}},
                "DecidedBy": {"select": {"name": "Agents — Policy Match" if review_type == "Release Confirmation" else "Manager Decision"}},
                "ConflictSummary": {"rich_text": [{"text": {"content": conflict_summary_text[:2000]}}]}
            }
        }

        try:
            headers = {
                "Authorization": f"Bearer {self._settings.notion_token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=15) as client:
                resp = client.post(f"{_NOTION_API}/pages", json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.error("notion_audit.create_approval_desk_entry_failed", error=str(exc))
            raise NotionAuditError(f"Failed to write approval desk entry: {exc}") from exc

    @property
    def _notion_audit_feed_id(self) -> str:
        return getattr(self._settings, "notion_audit_feed_id", "mock-audit-db")
