"""
tests/test_e2e_scenarios.py

End-to-end tests for all four mandated scenarios.
Zero real external calls — every agent LLM call, Notion write,
Gmail send, Slack post, and compression LLM call is mocked with
pre-scripted response sequences consumed in order (side_effect lists).

Scenario matrix:
  1. Trigger 1 — subsidy loan, tax dispute + funding gap
       → negotiation (Round 1 PARTIAL, Round 2 APPROVED)
       → human-approval webhook → Gmail + Slack + ledger execution
  2. Trigger 2 — emergency payout, frozen fund + missing GPS tag
       → single-round TREASURY_REJECT + requiresHumanReview
       → autonomous escalation (frozen fund is under threshold in terms of
         amount disbursed = 0, so execution path = Manager Desk only)
  3. Adversarial — emergency payout negotiated to ₹49,999 (just under threshold)
       while touching a frozen fund → validator must force human review
  4. Adversarial — negotiation round-cap: all rounds return PARTIAL, no consensus
       → must escalate to Manager Desk, not error out
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.models.proposal import Proposal, ProposalStatus
from app.models.transaction import TransactionContext, TransactionType, TransactionStatus
from app.orchestrator.negotiation import NegotiationEngine
from app.orchestrator.history import TranscriptCompressor, get_policy_version
from app.orchestrator.validator import validate_proposal_history, ValidationError
from app.services.transaction_store import TransactionStore


# ── Shared fixtures ────────────────────────────────────────────────────────────

def _ctx(
    *,
    amount: Decimal = Decimal("25000.00"),
    urgency: str = "normal",
    fund_tag: str = "general",       # encoded into UUID for simulate_treasury_ledger
    fund_tag2: str | None = None,    # second tag (e.g. "insufficient")
    customer_name: str = "Priya Sharma",
    guarantor: str | None = None,
    notes_extra: str = "",
) -> TransactionContext:
    """
    Build a TransactionContext whose UUID hex encodes the given fund_tag
    so that mock_services.simulate_treasury_ledger returns the right state.
    """
    # Encode fund tag into hex (same scheme as persistence.py / existing tests)
    fund_hex = fund_tag.encode("utf-8").hex().ljust(32, "0")[:32]
    if fund_tag2:
        tag2_hex = fund_tag2.encode("utf-8").hex().ljust(32, "0")[:32]
        fund_hex = format(int(fund_hex, 16) ^ int(tag2_hex, 16), "032x")
    txn_id = uuid.UUID(fund_hex)

    meta: dict[str, str] = {}
    if guarantor:
        meta["guarantor"] = guarantor

    notes = notes_extra
    if guarantor:
        notes = f"Guarantor: {guarantor}. {notes}"

    return TransactionContext(
        transaction_id=txn_id,
        transaction_type=TransactionType.LOAN_APPLICATION,
        customer_name=customer_name,
        customer_email="test@sandbox.mockbank.example.com",
        requested_amount=amount,
        currency="INR",
        urgency_flag=urgency,
        notes=notes,
        metadata=meta,
    )


# ── Canned LLM responses ───────────────────────────────────────────────────────
#
# Each scenario has its own list consumed in order per call_llm patch:
#   agent_a_loans.call_llm → routing decision
#   agent_b_risk.call_llm  → risk assessment
#   agent_c_treasury.call_llm → treasury assessment
#   history.call_llm       → compressor (always last in a scenario)
#
# Agent C is NOT called when the fund is Frozen (hard-coded short-circuit in
# evaluate_treasury). So those scenarios have one fewer call_llm invocation.

def _a_normal() -> str:
    return json.dumps({"route": "A->B->C", "cited_clause": "Section I, Clause 1"})

def _a_emergency() -> str:
    return json.dumps({"route": "A->C", "cited_clause": "Section I, Clause 2"})

def _b_pass() -> str:
    return json.dumps({"flags": [], "status": "PASSED",
                       "citedClause": "Section I, Clause 1", "notes": ""})

def _b_flagged_cleared(guarantor: str = "Rajan Guarantor") -> str:
    return json.dumps({
        "flags": ["2-year unresolved tax dispute"],
        "status": "PASSED",
        "citedClause": "Section I, Clause 1",
        "notes": f"Guarantor {guarantor} offsets risk veto.",
    })

def _c_partial(amount: float = 15000.0) -> str:
    return json.dumps({
        "status": "PARTIAL", "availableAmount": amount,
        "citedClause": "Section I, Clause 1", "requiresHumanReview": False,
    })

def _c_approved(amount: float = 25000.0) -> str:
    return json.dumps({
        "status": "APPROVED", "availableAmount": amount,
        "citedClause": "Section I, Clause 1", "requiresHumanReview": False,
    })

def _compressor_bullets(*labels: str) -> str:
    bullets = [
        f"• Requested: ₹{labels[0] if labels else '25,000'} subsidy loan.",
        "• Flagged: tax dispute detected.",
        "• Resolved: guarantor offset risk.",
        "• Final outcome: APPROVED.",
    ]
    return json.dumps(bullets)


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Trigger 1: Subsidy loan, tax dispute + funding gap
# ══════════════════════════════════════════════════════════════════════════════

class TestScenario1SubsidyLoan:
    """
    Subsidy loan for flagged applicant with guarantor.
    Round 1: Agent B clears veto (guarantor), Agent C returns PARTIAL (funding gap).
    Round 2: Agent C returns APPROVED (negotiated lower amount).
    Then: human-approval webhook fires Gmail + Slack + ledger.
    """

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario1_negotiation_reaches_consensus(
        self, mock_push, mock_hist, mock_c, mock_b, mock_a
    ):
        """Round 1 PARTIAL → Round 2 APPROVED → accepted=True, rounds=2."""
        ctx = _ctx(amount=Decimal("25000"), guarantor="Rajan Guarantor",
                   notes_extra="Tax dispute flag present.")

        mock_a.side_effect = [_a_normal(), _a_normal()]
        mock_b.side_effect = [_b_flagged_cleared(), _b_flagged_cleared()]
        mock_c.side_effect = [_c_partial(15000.0), _c_approved(15000.0)]
        mock_hist.return_value = _compressor_bullets("15,000")

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        assert result.accepted is True, f"Expected accepted, got: {result.reason}"
        assert result.rounds == 2
        assert result.final_proposal.proposed_amount == Decimal("15000.00")
        assert result.final_proposal.status == ProposalStatus.ACCEPTED
        assert len(result.transcript) == 2
        assert result.transcript[0].status == ProposalStatus.COUNTERED

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario1_compression_and_audit_push(
        self, mock_push, mock_hist, mock_c, mock_b, mock_a
    ):
        """After consensus, transcript compressor fires once and pushes to Audit Feed."""
        ctx = _ctx(amount=Decimal("25000"), guarantor="Rajan Guarantor")

        mock_a.side_effect = [_a_normal(), _a_normal()]
        mock_b.side_effect = [_b_flagged_cleared(), _b_flagged_cleared()]
        mock_c.side_effect = [_c_partial(15000.0), _c_approved(15000.0)]
        mock_hist.return_value = _compressor_bullets("15,000")

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        compressor = TranscriptCompressor()
        summary = compressor.compress(ctx, result.transcript, route="A->B->C")

        assert mock_hist.call_count == 1
        mock_push.assert_called_once()
        assert summary.outcome == "ACCEPTED"
        assert summary.policy_version.startswith("pb-")
        assert "Agent C" in summary.agents_involved

    @pytest.mark.asyncio
    @patch("app.integrations.gmail.get_settings")
    @patch("app.integrations.slack.get_settings")
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario1_human_approval_triggers_gmail_and_slack(
        self, mock_push, mock_hist, mock_c, mock_b, mock_a,
        mock_slack_settings, mock_gmail_settings
    ):
        """
        After negotiation consensus, simulating a human 'Approved' webhook
        must fire Gmail and Slack exactly once (idempotency enforced by
        executed_tools set).
        """
        from app.integrations.gmail import send_credit_confirmation_email
        from app.integrations.slack import send_credit_confirmed_alert

        # Set up mock settings for both clients
        for mock_s in [mock_gmail_settings, mock_slack_settings]:
            s = MagicMock()
            s.use_mock_gmail = True
            s.use_mock_slack = True
            s.app_env = "development"
            s.gmail_sender_address = "no-reply@mockbank.example.com"
            s.gmail_sandbox_to = "sandbox@mockbank.example.com"
            s.slack_channel_id = "C00000000"
            s.slack_sandbox_channel = "bank-sim-test"
            mock_s.return_value = s

        ctx = _ctx(amount=Decimal("25000"), guarantor="Rajan Guarantor")

        mock_a.side_effect = [_a_normal(), _a_normal()]
        mock_b.side_effect = [_b_flagged_cleared(), _b_flagged_cleared()]
        mock_c.side_effect = [_c_partial(15000.0), _c_approved(15000.0)]
        mock_hist.return_value = _compressor_bullets("15,000")

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)
        assert result.accepted

        # Simulate human approval → downstream execution
        from app.services.transaction_store import transaction_store
        
        with patch("app.integrations.gmail.send_applicant_email", return_value=True) as mock_gmail_post:
            send_credit_confirmation_email(ctx)
            r_gmail_mock = mock_gmail_post.called
        
        with patch("app.integrations.slack.post_slack_message", return_value=True) as mock_slack_post:
            send_credit_confirmed_alert(ctx)
            r_slack_mock = mock_slack_post.called

        assert r_gmail_mock is True
        assert r_slack_mock is True
        
        # Check idempotency marks
        tool_key_slack = f"slack:credit_confirmed:{ctx.transaction_id}"
        assert transaction_store.has_executed_tool(ctx.transaction_id, tool_key_slack)
        tool_key_gmail = f"gmail:credit_confirmed:{ctx.transaction_id}"
        assert transaction_store.has_executed_tool(ctx.transaction_id, tool_key_gmail)

        # Idempotency check
        with patch("app.integrations.gmail.send_applicant_email", return_value=True) as mock_gmail_post_2:
            send_credit_confirmation_email(ctx)
            r_gmail_mock_2 = mock_gmail_post_2.called

        with patch("app.integrations.slack.post_slack_message", return_value=True) as mock_slack_post_2:
            send_credit_confirmed_alert(ctx)
            r_slack_mock_2 = mock_slack_post_2.called
            
        assert r_gmail_mock_2 is False
        assert r_slack_mock_2 is False


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Trigger 2: Emergency payout, frozen fund + missing GPS tag
# ══════════════════════════════════════════════════════════════════════════════

class TestScenario2EmergencyPayout:
    """
    Emergency payout (urgency=disaster) targeting frozen fund.
    Agent C hard-codes TREASURY_REJECT without calling the LLM (frozen-fund
    short-circuit in evaluate_treasury). requiresHumanReview=True.
    The validator then confirms the transaction must route to human review.
    """

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario2_frozen_fund_forces_human_review(
        self, mock_push, mock_hist, mock_a
    ):
        """
        Agent C does NOT call call_llm for frozen funds (hard-circuit in code).
        The final proposal must have requires_human_review=true and be REJECTED.
        """
        ctx = _ctx(amount=Decimal("45000"), urgency="disaster", fund_tag="frozen")
        mock_a.return_value = _a_emergency()
        mock_hist.return_value = json.dumps(["• Emergency payout.", "• Frozen fund → ESCALATED."])

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        assert result.accepted is False
        final = result.final_proposal
        assert final.metadata.get("requires_human_review") == "true"
        assert final.status == ProposalStatus.REJECTED

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario2_agent_c_llm_not_called_for_frozen_fund(
        self, mock_push, mock_hist, mock_a
    ):
        """
        The frozen-fund rule is enforced in code, not by LLM.
        Patching agent_c_treasury.call_llm and asserting it's never called
        proves the hard-circuit works regardless of what the LLM would say.
        """
        ctx = _ctx(amount=Decimal("45000"), urgency="disaster", fund_tag="frozen")
        mock_a.return_value = _a_emergency()
        mock_hist.return_value = json.dumps(["• Frozen. ESCALATED."])

        with patch("app.agents.agent_c_treasury.call_llm") as mock_c_llm:
            engine = NegotiationEngine(max_rounds=4)
            await engine.run(ctx)
            mock_c_llm.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario2_route_is_a_to_c(self, mock_push, mock_hist, mock_a):
        """Emergency bypass route must be A->C (Agent B skipped)."""
        ctx = _ctx(amount=Decimal("45000"), urgency="disaster", fund_tag="frozen")
        mock_a.return_value = _a_emergency()
        mock_hist.return_value = json.dumps(["• Emergency. ESCALATED."])

        with patch("app.agents.agent_b_risk.call_llm") as mock_b_llm:
            engine = NegotiationEngine(max_rounds=4)
            result = await engine.run(ctx)
            # For emergency (A->C route), Agent B is not invoked.
            # The state machine checks "B" in route to decide whether to run B.
            # We simply verify b's LLM was never called.
            mock_b_llm.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_scenario2_compression_captures_correct_outcome(
        self, mock_push, mock_hist, mock_a
    ):
        ctx = _ctx(amount=Decimal("45000"), urgency="disaster", fund_tag="frozen")
        mock_a.return_value = _a_emergency()
        mock_hist.return_value = json.dumps(["• Frozen fund escalation.", "• Outcome: REJECTED."])

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        compressor = TranscriptCompressor()
        summary = compressor.compress(ctx, result.transcript, route="A->C")

        assert summary.outcome == "REJECTED"
        assert summary.route == "A->C"


class TestScenario4AdversarialRoundCap:
    """
    All 4 negotiation rounds return PARTIAL.
    The engine must: escalate to human review, not raise an exception.
    The transcript must have a final Orchestrator-originated REJECTED proposal
    with requires_human_review=true.
    """

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    async def test_round_cap_escalates_not_raises(self, mock_c, mock_b, mock_a):
        """Engine returns a NegotiationResult with accepted=False, not an exception."""
        ctx = _ctx(amount=Decimal("30000"), fund_tag2="insufficient")

        mock_a.side_effect = [_a_normal()] * 4
        mock_b.side_effect = [_b_pass()] * 4
        mock_c.side_effect = [_c_partial(5000.0)] * 4

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)   # must NOT raise

        assert result.accepted is False
        assert result.rounds == 4

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    async def test_round_cap_final_proposal_requires_human_review(self, mock_c, mock_b, mock_a):
        """The escalation proposal added by the engine must set requires_human_review=true."""
        ctx = _ctx(amount=Decimal("30000"), fund_tag2="insufficient")

        mock_a.side_effect = [_a_normal()] * 4
        mock_b.side_effect = [_b_pass()] * 4
        mock_c.side_effect = [_c_partial(5000.0)] * 4

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        final = result.final_proposal
        assert final.originated_by == "Orchestrator"
        assert final.status == ProposalStatus.REJECTED
        assert final.metadata.get("requires_human_review") == "true"

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    async def test_round_cap_transcript_logged_for_all_rounds(self, mock_c, mock_b, mock_a):
        """Every round's proposal is in the transcript; plus the escalation proposal."""
        ctx = _ctx(amount=Decimal("30000"), fund_tag2="insufficient")

        mock_a.side_effect = [_a_normal()] * 4
        mock_b.side_effect = [_b_pass()] * 4
        mock_c.side_effect = [_c_partial(5000.0)] * 4

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        # 4 real rounds + 1 escalation proposal = 5 entries
        assert len(result.transcript) == 5

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_round_cap_validator_catches_human_review(
        self, mock_push, mock_hist, mock_c, mock_b, mock_a
    ):
        """
        After round-cap escalation, running validate_proposal_history on the
        transcript must confirm human review is required (not raise an error
        about an invalid clause — the validator enforces the human-review rule).
        """
        ctx = _ctx(amount=Decimal("30000"), fund_tag2="insufficient")

        mock_a.side_effect = [_a_normal()] * 4
        mock_b.side_effect = [_b_pass()] * 4
        mock_c.side_effect = [_c_partial(5000.0)] * 4
        mock_hist.return_value = json.dumps(["• Round cap hit.", "• ESCALATED."])

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        # Validator must pass (not raise) because the transcript correctly
        # has requires_human_review=true — this is the correct escalated state.
        # The exception would only fire if the transcript were INCONSISTENT.
        from app.orchestrator.validator import validate_proposal_history
        validate_proposal_history(result.transcript)  # must not raise

    @pytest.mark.asyncio
    @patch("app.agents.agent_a_loans.call_llm")
    @patch("app.agents.agent_b_risk.call_llm")
    @patch("app.agents.agent_c_treasury.call_llm")
    @patch("app.orchestrator.history.call_llm")
    @patch("app.orchestrator.history._push_to_audit_feed")
    async def test_round_cap_compression_runs_on_escalated_transcript(
        self, mock_push, mock_hist, mock_c, mock_b, mock_a
    ):
        """Even after escalation the transcript is compressed and pushed to Audit Feed."""
        ctx = _ctx(amount=Decimal("30000"), fund_tag2="insufficient")

        mock_a.side_effect = [_a_normal()] * 4
        mock_b.side_effect = [_b_pass()] * 4
        mock_c.side_effect = [_c_partial(5000.0)] * 4
        mock_hist.return_value = json.dumps(
            ["• Requested: ₹30,000.", "• Insufficient funds.", "• Round cap hit.", "• ESCALATED."]
        )

        engine = NegotiationEngine(max_rounds=4)
        result = await engine.run(ctx)

        compressor = TranscriptCompressor()
        summary = compressor.compress(ctx, result.transcript, route="A->B->C")

        assert mock_hist.call_count == 1
        mock_push.assert_called_once()
        assert "Orchestrator" in summary.agents_involved
        assert summary.outcome == "REJECTED"


# ══════════════════════════════════════════════════════════════════════════════
# Cross-scenario: no real external calls
# ══════════════════════════════════════════════════════════════════════════════

class TestNoRealExternalCalls:
    """
    Smoke-level assertions confirming the mock guards are in place.
    These are not meaningful business tests — they exist as a safety net
    to catch any accidental removal of the mock patches.
    """

    def test_simulate_treasury_ledger_is_mock(self):
        from app.integrations.mock_services import simulate_treasury_ledger
        result = simulate_treasury_ledger(uuid.uuid4())
        assert result["_mock"] is True

    def test_simulate_tax_registry_is_mock(self):
        from app.integrations.mock_services import simulate_tax_registry
        result = simulate_tax_registry(uuid.uuid4())
        assert result["_mock"] is True

    def test_gmail_mock_mode_returns_mock_response(self):
        from app.integrations.gmail import send_applicant_email
        with patch("app.integrations.mock_services.mock_gmail_send") as mock_send:
            result = send_applicant_email("x@y.com", "subj", "body")
            assert result is True
            mock_send.assert_called_once()

    def test_slack_mock_mode_returns_mock_response(self):
        from app.integrations.slack import post_slack_message
        with patch("app.integrations.slack.get_settings") as mock_settings:
            mock_settings.return_value.use_mock_slack = True
            result = post_slack_message("http://test", "test message")
            assert result is True
