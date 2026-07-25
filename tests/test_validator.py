"""
tests/test_validator.py

Tests for the strict proposal validator.
"""

from decimal import Decimal
from uuid import uuid4
import pytest

from app.models.proposal import Proposal
from app.orchestrator.validator import validate_proposal_history, ValidationError


def _make_prop(amount: str, cited_clause: str, requires_review: bool = False, agent: str = "Agent A") -> Proposal:
    return Proposal(
        transaction_id=uuid4(),
        originated_by=agent,
        proposed_amount=Decimal(amount),
        metadata={
            "cited_clause": cited_clause,
            "requires_human_review": str(requires_review).lower(),
        }
    )


def test_validator_clean_history_passes():
    """Valid clauses, under threshold, no human review required -> passes silently."""
    history = [
        _make_prop("10000", "Section I, Clause 1"),
        _make_prop("10000", "Section I, Clause 2", agent="Agent B"),
    ]
    # Should not raise
    validate_proposal_history(history)


def test_validator_invalid_clause_raises():
    """An invalid or missing clause citation raises ValidationError."""
    history = [
        _make_prop("10000", "Section X, Clause 99"),
    ]
    with pytest.raises(ValidationError, match="invalid clause"):
        validate_proposal_history(history)


def test_validator_missing_clause_raises():
    history = [
        _make_prop("10000", ""),
    ]
    with pytest.raises(ValidationError, match="missing a cited_clause"):
        validate_proposal_history(history)
