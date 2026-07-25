"""
app/scripts/simulate_engine.py

Core simulation runner — shared between the CLI and the FastAPI endpoint.

Builds a TransactionContext from fixture data, runs it through the full
production pipeline (StateMachine → NegotiationEngine → TranscriptCompressor),
and returns a structured SimulationResult.

External services mocked (via .env USE_MOCK_* flags):
  - Notion API       (USE_MOCK_NOTION=true)
  - Gmail            (USE_MOCK_GMAIL=true)
  - Slack            (USE_MOCK_SLACK=true)
  - LLM / Gemini     (USE_MOCK_LLM=true)

The orchestrator pipeline (StateMachine, NegotiationEngine, Validator) runs
exactly as in production — no shortcuts, no fake orchestration logic here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import structlog

from app.models.transaction import TransactionContext, TransactionStatus
from app.orchestrator.negotiation import NegotiationEngine, NegotiationResult
from app.orchestrator.history import TranscriptCompressor
from app.orchestrator.verdict import generate_verdict_text
from app.scripts.persistence import build_transaction_context, FixtureError
from app.services.transaction_store import transaction_store

log = structlog.get_logger(__name__)


@dataclass
class SimulationResult:
    transaction_id: UUID
    trigger_type: str
    applicant_id: str
    applicant_name: str
    final_status: str
    route: str
    rounds: int
    outcome_reason: str
    requires_human_review: bool
    policy_version: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error: str | None = None


async def run_simulation(
    trigger_type: str,
    applicant_id: str,
    amount: float | None = None,
) -> SimulationResult:
    """
    Execute the full production orchestrator pipeline for the given trigger.

    Steps:
      1. Build TransactionContext from fixtures.
      2. Register in the shared TransactionStore.
      3. Run NegotiationEngine (which calls StateMachine per round).
      4. Compress the transcript via one LLM call (TranscriptCompressor).
      5. Return SimulationResult with all audit fields populated.

    Raises:
        FixtureError: if trigger_type or applicant_id are invalid.
    """
    ctx = build_transaction_context(trigger_type, applicant_id, amount)
    transaction_store.upsert(ctx)

    log.info(
        "simulate.start",
        trigger_type=trigger_type,
        applicant_id=applicant_id,
        transaction_id=str(ctx.transaction_id),
    )

    # Print immediately so CLI user sees the ID before waiting for pipeline.
    _print_transaction_id(ctx.transaction_id, trigger_type, applicant_id, ctx.customer_name)

    started_at = datetime.now(timezone.utc)
    error: str | None = None

    # ── Run the full production pipeline ──────────────────────────────────────
    try:
        engine = NegotiationEngine()
        result: NegotiationResult = await engine.run(ctx)
    except Exception as exc:
        log.error("simulate.pipeline_error", error=str(exc))
        error = str(exc)
        result = None  # type: ignore[assignment]

    # ── Determine route from the first Proposal in the transcript ─────────────
    route = _extract_route(result)
    
    from app.orchestrator.validator import evaluate_eligibility
    eligibility, triggers = evaluate_eligibility(ctx, result.transcript if result else [])

    # ── Compress transcript → push to Notion Audit Feed ───────────────────────
    policy_version = "unknown"
    summary = None
    if result and result.transcript:
        try:
            compressor = TranscriptCompressor()
            summary = compressor.compress(ctx, result.transcript, route=route)
            policy_version = summary.policy_version
        except Exception as exc:
            log.error("simulate.compression_error", error=str(exc))

    # ── Update shared store with final status ─────────────────────────────────
    if result:
        final_status = _map_final_status(result, eligibility)
    else:
        final_status = TransactionStatus.REJECTED
    transaction_store.set_status(ctx.transaction_id, final_status)
    verdict_text = generate_verdict_text(ctx)
    transaction_store.add_progress(ctx.transaction_id, 8, "Verdict issued", verdict_text)

    if final_status != TransactionStatus.ESCALATED and eligibility not in (EligibilityClassification.ELIGIBLE_AUTO, EligibilityClassification.ELIGIBLE_PENDING_REVIEW):
        from app.integrations.gmail import send_verdict_email
        send_verdict_email(ctx)

    # ── Push to Manager Approval Desk if eligible ────────────────────────────
    if eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW and summary:
        try:
            from app.integrations.notion_audit import NotionAuditClient
            client = NotionAuditClient()
            client.create_approval_desk_entry(ctx, summary, eligibility=eligibility, triggers=triggers)
            log.info("simulate.manager_desk_pushed", transaction_id=str(ctx.transaction_id))
            transaction_store.add_progress(ctx.transaction_id, 9, "Manager decision", "Awaiting human review")
        except Exception as exc:
            log.error("simulate.manager_desk_push_failed", error=str(exc))
            
        from app.integrations.slack import send_review_queue_alert
        send_review_queue_alert(ctx, summary)
            
    if eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW:
        transaction_store.add_progress(ctx.transaction_id, 10, "Credit/disbursement confirmed", "Pending manager approval")
    elif final_status == TransactionStatus.APPROVED:
        from app.services.downstream import execute_approved_transaction
        import asyncio
        updated_ctx = transaction_store.mark_credited(ctx.transaction_id, "System Auto-Approval")
        
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(execute_approved_transaction(updated_ctx))
        except RuntimeError:
            asyncio.run(execute_approved_transaction(updated_ctx))
            
        transaction_store.add_progress(
            ctx.transaction_id, 
            10, 
            "Credit/disbursement confirmed", 
            "Funds autonomously released by agents"
        )
        from app.integrations.slack import send_credit_confirmed_alert
        send_credit_confirmed_alert(updated_ctx)
        
        from app.integrations.gmail import send_credit_confirmation_email
        send_credit_confirmation_email(updated_ctx)
    elif final_status == TransactionStatus.REJECTED:
        transaction_store.add_progress(ctx.transaction_id, 10, "Credit/disbursement confirmed", "Disbursement halted")

    sim_result = SimulationResult(
        transaction_id=ctx.transaction_id,
        trigger_type=trigger_type,
        applicant_id=applicant_id,
        applicant_name=ctx.customer_name,
        final_status=str(final_status),
        route=route,
        rounds=result.rounds if result else 0,
        outcome_reason=result.reason if result else (error or "unknown"),
        requires_human_review=(eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW),
        policy_version=policy_version,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        error=error,
    )

    log.info(
        "simulate.complete",
        transaction_id=str(ctx.transaction_id),
        final_status=final_status,
        rounds=sim_result.rounds,
        eligibility=eligibility.value,
    )
    return sim_result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_transaction_id(
    txn_id: UUID,
    trigger_type: str,
    applicant_id: str,
    name: str,
) -> None:
    """Print the transaction ID immediately so the user can watch Notion live."""
    print(
        f"\n{'='*60}\n"
        f"  🚀  SIMULATION STARTED\n"
        f"{'='*60}\n"
        f"  Transaction ID : {txn_id}\n"
        f"  Trigger        : {trigger_type}\n"
        f"  Applicant      : {applicant_id} — {name}\n"
        f"{'='*60}\n"
        f"  ↳ Watch your Notion dashboard for this TransactionID.\n"
        f"{'='*60}\n",
        flush=True,
    )


def _extract_route(result: NegotiationResult | None) -> str:
    if result is None:
        return "unknown"
    for proposal in result.transcript:
        # route is stored in the first Agent A proposal's notes or as a standard string
        if proposal.originated_by in ("Agent A",):
            return "A->B->C"  # default; state machine captures route in logs
    # Infer from agents in transcript
    agents = {p.originated_by for p in result.transcript}
    if "Agent B" in agents:
        return "A->B->C"
    if "Agent C" in agents:
        return "A->C"
    return "A->B->C"


from app.models.transaction import EligibilityClassification

def _map_final_status(result: NegotiationResult, eligibility: EligibilityClassification) -> TransactionStatus:
    if eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW:
        return TransactionStatus.ESCALATED
    if eligibility in (EligibilityClassification.NEEDS_ALTERATION_SUBSIDY_MISMATCH, EligibilityClassification.NEEDS_ALTERATION_INSUFFICIENT_FUNDS):
        return TransactionStatus.REJECTED
        
    if not result.accepted:
        # Agent autonomous rejection (e.g., ambiguity, unmet criteria)
        return TransactionStatus.REJECTED
        
    if eligibility == EligibilityClassification.ELIGIBLE_AUTO:
        # Autonomous approval (bypasses human manager)
        return TransactionStatus.APPROVED
        
    return TransactionStatus.REJECTED
