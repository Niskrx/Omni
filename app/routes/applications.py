import asyncio
from typing import Optional
from uuid import UUID, uuid4
from decimal import Decimal
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.models.transaction import TransactionContext, TransactionType, TransactionStatus, EligibilityClassification
from app.models.customer_status import CustomerStatusResponse, CustomerStep
from app.services.transaction_store import transaction_store

router = APIRouter()

class ApplicationCreate(BaseModel):
    customer_name: str
    loan_type: str
    requested_amount: float
    annual_declared_income: float
    is_urgent: bool
    target_fund: str
    credit_score: int
    marital_status: str
    dependents: int
    transaction_id: Optional[UUID] = None
    customer_id: Optional[UUID] = None
    notes: Optional[str] = ""
    metadata: Optional[dict[str, str]] = None

class ApplicationResponse(BaseModel):
    transaction_id: UUID

@router.post("/applications", response_model=ApplicationResponse)
async def create_application(app_data: ApplicationCreate, background_tasks: BackgroundTasks):
    applicant_id = f"APP-DASH-{uuid4().hex[:8]}"
    transaction_id = app_data.transaction_id or uuid4()
    
    # Map loan type string to enum
    try:
        transaction_type = TransactionType(app_data.loan_type)
    except ValueError:
        transaction_type = TransactionType.LOAN_APPLICATION
        
    ctx = TransactionContext(
        transaction_id=transaction_id,
        transaction_type=transaction_type,
        customer_id=app_data.customer_id or uuid4(),
        customer_name=app_data.customer_name,
        customer_email=f"{app_data.customer_name.lower().replace(' ', '.')}@example.com",
        requested_amount=Decimal(str(app_data.requested_amount)),
        annual_declared_income=Decimal(str(app_data.annual_declared_income)),
        urgency_flag="emergency" if app_data.is_urgent else "normal",
        credit_score=app_data.credit_score,
        marital_status=app_data.marital_status,
        dependents=app_data.dependents,
        mock_treasury_pool=app_data.target_fund,  # Use the selected target fund
        notes=app_data.notes or "",
        metadata=app_data.metadata or {}
    )
    
    # Upsert immediately so status endpoint finds it
    transaction_store.upsert(ctx)
    transaction_store.add_progress(transaction_id, 1, "Application received & validated", f"Applicant: {app_data.customer_name}")
    
    background_tasks.add_task(_run_dashboard_pipeline, ctx, applicant_id)
    
    return ApplicationResponse(transaction_id=transaction_id)

async def _run_dashboard_pipeline(ctx: TransactionContext, applicant_id: str):
    from app.scripts.simulate_engine import _extract_route, _map_final_status
    from app.orchestrator.negotiation import NegotiationEngine
    from app.orchestrator.history import TranscriptCompressor
    from app.orchestrator.verdict import generate_verdict_text
    from app.models.transaction import TransactionStatus
    import structlog
    from datetime import datetime, timezone

    log = structlog.get_logger(__name__)
    log.info("simulate.start", trigger_type="dashboard", applicant_id=applicant_id, transaction_id=str(ctx.transaction_id))

    error = None
    try:
        engine = NegotiationEngine()
        result = await engine.run(ctx)
    except Exception as exc:
        log.error("simulate.pipeline_error", error=str(exc))
        error = str(exc)
        result = None

    route = _extract_route(result)
    
    from app.orchestrator.validator import evaluate_eligibility
    eligibility, triggers = evaluate_eligibility(ctx, result.transcript if result else [])

    policy_version = "unknown"
    summary = None
    if result and result.transcript:
        try:
            compressor = TranscriptCompressor()
            summary = compressor.compress(ctx, result.transcript, route=route)
            policy_version = summary.policy_version
        except Exception as exc:
            log.error("simulate.compression_error", error=str(exc))

    if result:
        final_status = _map_final_status(result, eligibility)
    else:
        final_status = TransactionStatus.REJECTED

    transaction_store.set_status(ctx.transaction_id, final_status)
    verdict_text = generate_verdict_text(ctx)
    transaction_store.add_progress(ctx.transaction_id, 8, "Verdict issued", verdict_text)

    if final_status != TransactionStatus.ESCALATED:
        from app.integrations.gmail import send_verdict_email
        send_verdict_email(ctx)

    if (eligibility == EligibilityClassification.ELIGIBLE_AUTO or eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW) and summary:
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

    if eligibility == EligibilityClassification.ELIGIBLE_AUTO or eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW:
        transaction_store.add_progress(ctx.transaction_id, 10, "Credit/disbursement confirmed", "Pending manager approval")
    elif final_status == TransactionStatus.APPROVED:
        transaction_store.add_progress(ctx.transaction_id, 10, "Credit/disbursement confirmed", "Awaiting credit confirmation")
    elif final_status == TransactionStatus.REJECTED:
        transaction_store.add_progress(ctx.transaction_id, 10, "Credit/disbursement confirmed", "Disbursement halted")

@router.get("/applications/{transaction_id}/status")
def get_application_status(transaction_id: UUID):
    ctx = transaction_store.get(transaction_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    progress = transaction_store.get_progress(transaction_id)
    return {
        "transaction_id": transaction_id,
        "status": ctx.status,
        "steps": progress,
        "requires_human_review": any(s["step_num"] == 9 for s in progress) or ctx.status == "escalated",
        "credited_by": ctx.credited_by,
        "credited_at": ctx.credited_at.isoformat() if ctx.credited_at else None
    }

def get_required_documents(loan_type: str) -> list[str]:
    docs = ["Proof of Identity", "Recent Bank Statements", "Tax Returns (Last 2 Years)"]
    if loan_type == "emergency_payout":
        docs.append("Proof of Emergency (Medical/Disaster)")
    elif loan_type == "subsidy_loan":
        docs.append("Subsidy Eligibility Certificate")
    return docs

@router.get("/applications/{transaction_id}/customer-status", response_model=CustomerStatusResponse)
def get_customer_application_status(transaction_id: UUID):
    ctx = transaction_store.get(transaction_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    progress = transaction_store.get_progress(transaction_id)
    step_nums = {s["step_num"] for s in progress}
    
    # Needs alteration skips step 7
    is_needs_alteration = ctx.status == TransactionStatus.REJECTED and any(
        s["step_num"] == 8 and ("adjustment" in s.get("detail", "").lower() or "subsidy" in s.get("detail", "").lower() or "insufficient" in s.get("detail", "").lower()) for s in progress
    )
    # Wait, how do we reliably check if it's needs_alteration?
    # We can check the actual eligibility classification if we stored it, but we didn't store eligibility.
    # Alternatively, if ctx.status is REJECTED and ctx.decision_maker is None (agent automated rejection).
    is_agent_rejected = ctx.status == TransactionStatus.REJECTED and ctx.decision_maker is None
    
    # 7 fixed steps for the customer
    customer_steps_labels = [
        "Verifying applicant details",
        "Checking loan type & routing against policy",
        "Running risk & compliance check",
        "Checking fund availability",
        "Checking proportionality — loan amount vs. declared income",
        "Final policy validation",
        "Manager confirmation to release funds"
    ]
    
    steps = []
    
    # Map progress to these 7 steps
    # We can just use the fact that if the transaction is APPROVED or REJECTED or ESCALATED, it has passed certain points.
    
    # Heuristics for progress:
    # 1: Application received -> Step 1 completed
    # Agent A -> Step 2
    # Agent B -> Step 3
    # Agent C -> Step 4
    # Validator -> Step 5 & 6
    # Manager / Credit -> Step 7
    
    current_idx = 0
    if 1 in step_nums: current_idx = 1
    # If 8 (Verdict issued) is present, it means the automated part is done.
    if 8 in step_nums: current_idx = 6
    if ctx.status in [TransactionStatus.APPROVED, TransactionStatus.REJECTED]:
        current_idx = 7
        
    # If escalated, it's sitting at step 7 pending.
    if ctx.status == TransactionStatus.ESCALATED:
        current_idx = 6
        
    for i, label in enumerate(customer_steps_labels):
        if is_agent_rejected and i == 6:
            # Skip step 7 entirely for needs_alteration
            continue
            
        status = "pending"
        if i < current_idx:
            status = "completed"
        elif i == current_idx:
            if ctx.status in [TransactionStatus.APPROVED, TransactionStatus.REJECTED]:
                status = "completed"
            elif ctx.status == TransactionStatus.ESCALATED and i == 6:
                status = "pending" # sitting at 7
            else:
                status = "pending"
                
        steps.append({
            "step_num": i + 1,
            "name": label,
            "status": status
        })
        
    # Final message
    final_message = None
    if ctx.status in [TransactionStatus.APPROVED, TransactionStatus.REJECTED]:
        if ctx.decision_maker is None:
            if ctx.status == TransactionStatus.REJECTED:
                # needs_alteration
                # Try to extract the reason
                reason = "your request does not meet our guidelines"
                suggestion = "Please review our policy and apply again."
                for s in progress:
                    if s["step_num"] == 8:
                        reason = s["detail"]
                final_message = f"⚠️ Your request needs an adjustment: {reason}. Suggested fix: {suggestion}."
            else:
                final_message = f"✅ The request meets our policy requirements and has been processed. ₹{ctx.requested_amount} credited."
        else:
            if ctx.status == TransactionStatus.APPROVED:
                final_message = f"✅ Manager {ctx.decision_maker} approved your request. ₹{ctx.requested_amount} credited."
                if ctx.decision_reason:
                    final_message += f" Note: {ctx.decision_reason}"
            else:
                final_message = f"❌ Manager {ctx.decision_maker} was unable to approve your request at this time."
                if ctx.decision_reason:
                    final_message += f" Reason: {ctx.decision_reason}"
                
    return {
        "transaction_id": str(transaction_id),
        "status": ctx.status,
        "final_message": final_message,
        "steps": steps,
        "required_documents": get_required_documents(ctx.transaction_type.value)
    }

class ApplicationDecision(BaseModel):
    action: str  # "Approved" or "Rejected"
    manager_note: str | None = None

@router.post("/applications/{transaction_id}/decision")
async def application_decision(transaction_id: UUID, decision: ApplicationDecision):
    from app.routes.webhooks import NotionStatusChangePayload, _handle_approved, _handle_rejected
    ctx = transaction_store.get(transaction_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    if transaction_store.is_resolved(transaction_id):
        raise HTTPException(status_code=409, detail="Transaction already resolved")

    payload = NotionStatusChangePayload(
        transaction_id=transaction_id,
        new_status=decision.action,
        manager_note=decision.manager_note,
        actor="dashboard_user"
    )

    if decision.action == "Approved":
        await _handle_approved(payload, ctx)
    elif decision.action == "Rejected":
        _handle_rejected(payload)
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
        
    return {"status": "ok"}

@router.post("/applications/{transaction_id}/credit")
async def confirm_credit(transaction_id: UUID):
    ctx = transaction_store.get(transaction_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    from app.models.transaction import TransactionStatus
    if ctx.status != TransactionStatus.APPROVED:
        raise HTTPException(status_code=409, detail="Transaction is not approved and ready for credit")
        
    if ctx.credited_by is not None:
        return {"status": "already_credited"}
        
    # Mark as credited
    updated_ctx = transaction_store.mark_credited(transaction_id, "Manager")
    
    # Call downstream
    from app.services.downstream import execute_approved_transaction
    await execute_approved_transaction(updated_ctx)
    
    # Update progress
    transaction_store.add_progress(
        transaction_id, 
        10, 
        "Credit/disbursement confirmed", 
        f"Credited by {updated_ctx.credited_by} at {updated_ctx.credited_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    
    from app.integrations.slack import send_credit_confirmed_alert
    send_credit_confirmed_alert(updated_ctx)

    from app.integrations.gmail import send_credit_confirmation_email
    send_credit_confirmation_email(updated_ctx)
    
    return {"status": "credited"}
