import asyncio, httpx
async def main():
    async with httpx.AsyncClient() as client:
        r = await client.post("http://localhost:8000/api/v1/applications", json={
            "customer_name": "Test User",
            "loan_type": "standard_loan",
            "requested_amount": 10000,
            "annual_declared_income": 50000,
            "is_urgent": False,
            "target_fund": "General Subsidy Pool"
        })
        print(r.json())
        await asyncio.sleep(5)
if __name__ == "__main__":
    asyncio.run(main())
