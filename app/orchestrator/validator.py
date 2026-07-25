"""
app/orchestrator/validator.py

Pure Python validator. No LLM calls.
Validates agent proposals before they are executed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Sequence

from app.models.proposal import Proposal, ProposalStatus
from app.models.transaction import TransactionContext, EligibilityClassification
from app.orchestrator.judgment import (
    calculate_household_adjustment_factor,
    calculate_proportionality_ratio,
    is_disproportionate,
    is_credit_score_flagged,
)


class ValidationError(Exception):
    """Raised when a proposal violates policy rules."""


def _get_valid_clauses() -> set[str]:
    """Parse policy_book.md and extract valid citations like 'Section I, Clause 2'."""
    policy_path = Path(__file__).parent.parent / "agents" / "policy_book.md"
    try:
        content = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()

    valid_clauses = set()
    current_section = None
    
    for line in content.splitlines():
        if line.startswith("# Section "):
            # e.g. "# Section I: Standard Loan Routing" -> "Section I"
            current_section = line.split(":")[0].strip("# ")
        elif line.startswith("- **Clause "):
            # e.g. "- **Clause 1**: ..." -> "Clause 1"
            clause = line.split("**:")[0].strip("- *")
            if current_section:
                valid_clauses.add(f"{current_section}, {clause}")
                
    return valid_clauses


def validate_proposal_history(proposals: Sequence[Proposal], ctx: TransactionContext | None = None) -> None:
    """
    Validates a transaction history of agent proposals.
    
    Rules:
      1. Every proposal must cite a valid clause from the Policy Book.
    """
    if not proposals:
        return

    valid_clauses = _get_valid_clauses()

    for i, prop in enumerate(proposals):
        cited = prop.metadata.get("cited_clause", "").strip()
        if not cited:
            raise ValidationError(f"Proposal {prop.proposal_id} by {prop.originated_by} is missing a cited_clause.")
        
        if cited not in valid_clauses:
            raise ValidationError(
                f"Proposal {prop.proposal_id} cited an invalid clause: '{cited}'. "
                f"Valid clauses are: {sorted(list(valid_clauses))}"
            )

    if ctx is not None:
        from app.services.transaction_store import transaction_store
        
        prop_ratio_note = "PASSED — Proportionate"
        try:
            haf = calculate_household_adjustment_factor(ctx.marital_status == "Married", ctx.dependents)
            pr = calculate_proportionality_ratio(float(proposals[-1].proposed_amount), float(ctx.annual_declared_income), haf)
            if is_disproportionate(pr):
                prop_ratio_note = "FAILED — PR > 0.30"
        except ValueError:
            prop_ratio_note = "FAILED — Invalid Income"
            
        transaction_store.add_progress(ctx.transaction_id, 5, "Income Proportionality Score", prop_ratio_note)
        transaction_store.add_progress(ctx.transaction_id, 7, "Validator — final determination", "PASSED")


def evaluate_eligibility(ctx: TransactionContext, transcript: Sequence[Proposal]) -> tuple[EligibilityClassification, list[str]]:
    """
    Replaces boolean requires_human_review with a 4-way eligibility enum.
    Stores all applicable trigger reasons.
    """
    triggers = []
    
    if not transcript:
        return EligibilityClassification.ELIGIBLE_AUTO, []
        
    final_prop = transcript[-1]
    
    # Needs alteration (Instant Reject, No human)
    # 1. Insufficient funds (Agent C rejects but it's not Frozen)
    # 2. Subsidy mismatch (Agent A rejects due to subsidy)
    for p in transcript:
        if p.status == ProposalStatus.REJECTED:
            if p.originated_by == "Agent C" and "Frozen" not in p.rationale:
                return EligibilityClassification.NEEDS_ALTERATION_INSUFFICIENT_FUNDS, ["Insufficient funds (Clause 5)"]
            if p.originated_by == "Agent A" and "subsidy" in p.rationale.lower():
                return EligibilityClassification.NEEDS_ALTERATION_SUBSIDY_MISMATCH, ["Subsidy mismatch (Clause 3)"]

    # eligible_pending_review triggers
    if final_prop.proposed_amount > Decimal("50000"):
        triggers.append("Amount > 50000")
        
    try:
        haf = calculate_household_adjustment_factor(ctx.marital_status == "Married", ctx.dependents)
        pr = calculate_proportionality_ratio(float(final_prop.proposed_amount), float(ctx.annual_declared_income), haf)
        if is_disproportionate(pr):
            triggers.append("PR > 0.30")
    except ValueError:
        triggers.append("Invalid income")
        
    if is_credit_score_flagged(ctx.credit_score):
        triggers.append("Credit score < 650")
        
    for p in transcript:
        if p.originated_by == "Agent C" and p.status == ProposalStatus.REJECTED and "Frozen" in p.rationale:
            triggers.append("Frozen fund")
        if p.originated_by == "Agent B" and p.status == ProposalStatus.REJECTED:
            triggers.append("Unresolved flag with no guarantor")
            
    if final_prop.originated_by == "Orchestrator" and final_prop.status == ProposalStatus.REJECTED:
        if "Round limit" in final_prop.rationale:
            triggers.append("Negotiation round cap reached")
            
    if triggers:
        return EligibilityClassification.ELIGIBLE_PENDING_REVIEW, triggers
        
    return EligibilityClassification.ELIGIBLE_AUTO, []
