import pytest
from decimal import Decimal
import json
from uuid import uuid4
from unittest.mock import patch, MagicMock, AsyncMock

from app.models.transaction import TransactionContext, TransactionType, TransactionStatus
from app.models.proposal import Proposal, ProposalStatus
from app.orchestrator.judgment import calculate_proportionality_ratio, is_disproportionate
from app.orchestrator.validator import evaluate_eligibility
from app.models.transaction import EligibilityClassification
from app.scripts.simulate_engine import run_simulation
from app.services.transaction_store import transaction_store

# ── 1. Proportionality Boundaries ─────────────────────────────────────────────

def test_proportionality_boundaries():
    # Exactly 0.30 -> False (clear)
    assert calculate_proportionality_ratio(30000, 100000, 1.0) == 0.30
    assert not is_disproportionate(0.30)
    
    # 0.3000001 -> True (fail)
    assert is_disproportionate(0.3000001)

    # 0.29 -> False
    assert not is_disproportionate(0.29)
    
    with pytest.raises(ValueError):
        calculate_proportionality_ratio(30000, 0, 1.0)
    
    with pytest.raises(ValueError):
        calculate_proportionality_ratio(30000, -100, 1.0)

# ── 2. triggers for eligible_pending_review ───────────────────────────────────

def test_eligible_pending_review_triggers():
    ctx = TransactionContext(
        transaction_id=uuid4(),
        transaction_type=TransactionType.LOAN_APPLICATION,
        customer_name="Test", customer_email="t@t.com",
        requested_amount=Decimal("10000"),
        annual_declared_income=Decimal("100000"),
        urgency_flag="normal",
        notes=""
    )
    
    # Test amount > 50k
    p1 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent C", status=ProposalStatus.ACCEPTED, proposed_amount=Decimal("50001"), rationale="")
    eligibility, triggers = evaluate_eligibility(ctx, [p1])
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW
    assert "Amount > 50000" in triggers
    
    # Test PR > 0.30
    p2 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent C", status=ProposalStatus.ACCEPTED, proposed_amount=Decimal("35000"), rationale="")
    eligibility, triggers = evaluate_eligibility(ctx, [p2])
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW
    assert "PR > 0.30" in triggers
    
    # Test frozen fund
    p3 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent C", status=ProposalStatus.REJECTED, proposed_amount=Decimal("10000"), rationale="Frozen fund.")
    eligibility, triggers = evaluate_eligibility(ctx, [p3])
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW
    assert "Frozen fund" in triggers
    
    # Test unresolved flag with no guarantor
    p4 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent B", status=ProposalStatus.REJECTED, proposed_amount=Decimal("10000"), rationale="Risk flag.")
    p5 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent C", status=ProposalStatus.ACCEPTED, proposed_amount=Decimal("10000"), rationale="")
    eligibility, triggers = evaluate_eligibility(ctx, [p4, p5])
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW
    assert "Unresolved flag with no guarantor" in triggers
    
    # Test negotiation round cap
    p6 = Proposal(transaction_id=ctx.transaction_id, originated_by="Orchestrator", status=ProposalStatus.REJECTED, proposed_amount=Decimal("10000"), rationale="Round limit reached.")
    eligibility, triggers = evaluate_eligibility(ctx, [p6])
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW
    assert "Negotiation round cap reached" in triggers

# ── 3. eligible_auto behavior ─────────────────────────────────────────────────

def test_eligible_auto_creates_release_confirmation_card():
    ctx = TransactionContext(
        transaction_id=uuid4(),
        transaction_type=TransactionType.LOAN_APPLICATION,
        customer_name="Test", customer_email="t@t.com",
        requested_amount=Decimal("10000"),
        annual_declared_income=Decimal("100000"),
        urgency_flag="normal",
        notes=""
    )
    # Under 50k, PR = 0.10, no rejections
    p1 = Proposal(transaction_id=ctx.transaction_id, originated_by="Agent C", status=ProposalStatus.ACCEPTED, proposed_amount=Decimal("10000"), rationale="")
    eligibility, triggers = evaluate_eligibility(ctx, [p1])
    assert eligibility == EligibilityClassification.ELIGIBLE_AUTO
    assert triggers == []

# ── 4. ConflictSummary no LLM coupling ────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.agents.llm_client.call_llm")
@patch("app.integrations.notion_audit.NotionAuditClient.create_approval_desk_entry")
@patch("app.integrations.notion_audit.NotionAuditClient.create_audit_entry")
async def test_conflict_summary_no_llm_coupling(mock_audit, mock_desk, mock_llm):
    # LLM fails for compressor
    from app.agents.llm_client import LLMUnavailableError
    
    def llm_side_effect(prompt, user_msg):
        # We need the negotiation LLMs to work, but compressor to fail
        if "compliance summariser" in prompt:
            raise LLMUnavailableError("LLM offline")
        # Agent A
        if "Agent A" in prompt:
            return json.dumps({"route": "A->B->C", "cited_clause": "Section I, Clause 1"})
        # Agent B
        if "Agent B" in prompt:
            return json.dumps({"flags": [], "status": "PASSED", "citedClause": "Section I, Clause 1", "notes": ""})
        # Agent C (Return a trigger for review so it escalates)
        if "Agent C" in prompt:
            return json.dumps({"status": "APPROVED", "availableAmount": 55000.0, "citedClause": "Section I, Clause 1", "requiresHumanReview": False})
        return "{}"
        
    mock_llm.side_effect = llm_side_effect
    
    # Run simulation with amount 55000 -> triggers amount > 50000 -> Pending Review -> Escalated
    res = await run_simulation("subsidy_loan", "APP-001", amount=55000.0)
    
    assert res.requires_human_review is True
    assert res.final_status == "escalated"
    
    # Check that audit feed contains the fallback string
    mock_audit.assert_called_once()
    summary = mock_audit.call_args.kwargs["summary"]
    assert any("LLM offline" in b for b in summary.bullets)
    
    # Check that desk entry was created with ReviewType = Judgment Required
    mock_desk.assert_called_once()
    eligibility = mock_desk.call_args.kwargs["eligibility"]
    assert eligibility == EligibilityClassification.ELIGIBLE_PENDING_REVIEW

# ── 5. Fund release requires manager action ───────────────────────────────────

from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)

def test_fund_release_requires_manager_action():
    # Create an eligible_auto transaction
    ctx = TransactionContext(
        transaction_id=uuid4(),
        transaction_type=TransactionType.LOAN_APPLICATION,
        customer_name="Test", customer_email="t@t.com",
        requested_amount=Decimal("10000"),
        annual_declared_income=Decimal("100000"),
        urgency_flag="normal",
        notes=""
    )
    transaction_store.upsert(ctx)
    transaction_store.set_status(ctx.transaction_id, TransactionStatus.ESCALATED)
    
    # Attempt to credit without manager action
    resp = client.post(f"/api/v1/applications/{ctx.transaction_id}/credit")
    assert resp.status_code == 409
    assert "not approved" in resp.json()["detail"]
    
    # Simulate Manager Action
    from app.routes.webhooks import NotionStatusChangePayload, _handle_approved
    import asyncio
    
    payload = NotionStatusChangePayload(
        transaction_id=ctx.transaction_id,
        new_status="Approved",
        manager_note="Looks good",
        actor="manager"
    )
    
    # Call internal handler directly since webhook might need auth mock
    asyncio.run(_handle_approved(payload, ctx))
    
    # Now credit should succeed
    with patch("app.services.downstream.execute_approved_transaction", new_callable=AsyncMock):
        resp = client.post(f"/api/v1/applications/{ctx.transaction_id}/credit")
        assert resp.status_code == 200
        assert resp.json()["status"] == "credited"
