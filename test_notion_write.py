import asyncio
from uuid import uuid4
from decimal import Decimal
from app.models.transaction import TransactionContext, TransactionType, EligibilityClassification
from app.integrations.notion_audit import NotionAuditClient

async def main():
    ctx = TransactionContext(
        transaction_id=uuid4(),
        transaction_type=TransactionType.LOAN_APPLICATION,
        customer_name="Diagnostic Test",
        customer_email="test@example.com",
        requested_amount=Decimal("30000"),
        annual_declared_income=Decimal("50000")
    )
    from app.orchestrator.history import CompressedSummary
    summary = CompressedSummary(
        transaction_id=ctx.transaction_id,
        bullets=["Test"], agents_involved=[], route="A", outcome="Test", policy_version="Test"
    )
    client = NotionAuditClient()
    try:
        resp = client.create_approval_desk_entry(ctx, summary, EligibilityClassification.ELIGIBLE_PENDING_REVIEW, ["Test Trigger"])
        print("Success:", resp)
    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
