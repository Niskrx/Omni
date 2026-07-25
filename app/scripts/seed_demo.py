import asyncio
import httpx
import uuid

BACKEND_URL = "http://localhost:10000/api/v1/applications"

scenarios = [
    {
        "name": "1. Fast Lane Pass (Clean & Auto-Approved)",
        "payload": {
            "customer_name": "Fast Lane Applicant",
            "loan_type": "Personal",
            "requested_amount": 25000.0,
            "annual_declared_income": 70000.0,
            "is_urgent": False,
            "target_fund": "General Subsidy Pool"
        }
    },
    {
        "name": "2. Review Queue: Income Proportionality Case (Elevated Band)",
        "payload": {
            "customer_name": "Proportionality Review Applicant",
            "loan_type": "Personal",
            "requested_amount": 30000.0,
            "annual_declared_income": 40000.0,
            "is_urgent": False,
            "target_fund": "General Subsidy Pool"
        }
    },
    {
        "name": "3. Review Queue: Frozen Fund Case (Emergency Route)",
        "payload": {
            "customer_name": "Frozen Fund Applicant",
            "loan_type": "Emergency Agriculture Relief",
            "requested_amount": 20000.0,
            "annual_declared_income": 80000.0,
            "is_urgent": True,
            "target_fund": "Branch Cash Reserve",
            # hex string contains "66726f7a656e" (frozen)
            "transaction_id": "66726f7a-656e-4000-8000-000000000000"
        }
    },
    {
        "name": "4. Review Queue: Tax Dispute & Guarantor Case",
        "payload": {
            "customer_name": "Tax Dispute Applicant",
            "loan_type": "Business Expansion",
            "requested_amount": 35000.0,
            "annual_declared_income": 85000.0,
            "is_urgent": False,
            "target_fund": "General Subsidy Pool",
            # customer_id contains "666c6167676564" (flagged)
            "customer_id": "666c6167-6765-6400-8000-000000000000",
            "notes": "Guarantor: Rajan Guarantor. Standard expansion proposal.",
            "metadata": {"guarantor": "Rajan Guarantor"}
        }
    },
    {
        "name": "5. Review Queue: Round Cap Negotiation Escalation Case",
        "payload": {
            "customer_name": "Round Cap Applicant",
            "loan_type": "Personal",
            "requested_amount": 30000.0,
            "annual_declared_income": 80000.0,
            "is_urgent": False,
            "target_fund": "General Subsidy Pool",
            # transaction_id contains "deadbeef" (insufficient fund roundcap test)
            "transaction_id": "deadbeef-0000-4000-8000-000000000000"
        }
    }
]

async def seed():
    async with httpx.AsyncClient() as client:
        for scen in scenarios:
            print(f"Firing scenario: {scen['name']}...")
            try:
                res = await client.post(BACKEND_URL, json=scen["payload"], timeout=10.0)
                if res.status_code == 200:
                    data = res.json()
                    print(f"  -> SUCCESS! Transaction ID: {data['transaction_id']}")
                else:
                    print(f"  -> FAILED! Status code: {res.status_code}, Body: {res.text}")
            except Exception as e:
                print(f"  -> ERROR: {e}")
            print("-" * 50)

if __name__ == "__main__":
    asyncio.run(seed())
