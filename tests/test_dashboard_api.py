import pytest
from fastapi.testclient import TestClient
from uuid import UUID

from app.main import app
from app.services.transaction_store import transaction_store

client = TestClient(app)

def test_create_application():
    payload = {
        "customer_name": "Test User",
        "loan_type": "personal",
        "requested_amount": 10000,
        "annual_declared_income": 50000,
        "is_urgent": False,
        "target_fund": "General Subsidy Pool",
        "credit_score": 750,
        "marital_status": "Single",
        "dependents": 0
    }
    
    response = client.post("/api/v1/applications", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert "transaction_id" in data
    
    # Check if it was added to the store immediately
    txn_id = UUID(data["transaction_id"])
    ctx = transaction_store.get(txn_id)
    assert ctx is not None
    assert ctx.customer_name == "Test User"
    
    # Check status endpoint
    status_response = client.get(f"/api/v1/applications/{txn_id}/status")
    assert status_response.status_code == 200
    
    status_data = status_response.json()
    assert status_data["transaction_id"] == str(txn_id)
    assert len(status_data["steps"]) >= 1
    assert status_data["steps"][0]["step_num"] == 1
    assert "Test User" in status_data["steps"][0]["detail"]

def test_customer_status_sanitized():
    payload = {
        "customer_name": "Test User",
        "loan_type": "standard_loan",
        "requested_amount": 10000,
        "annual_declared_income": 50000,
        "is_urgent": False,
        "target_fund": "General Subsidy Pool",
        "credit_score": 750,
        "marital_status": "Single",
        "dependents": 0
    }
    
    response = client.post("/api/v1/applications", json=payload)
    assert response.status_code == 200
    txn_id = UUID(response.json()["transaction_id"])
    
    # Check customer-status endpoint
    c_status_response = client.get(f"/api/v1/applications/{txn_id}/customer-status")
    assert c_status_response.status_code == 200
    
    c_data = c_status_response.json()
    assert "transaction_id" in c_data
    assert "steps" in c_data
    assert len(c_data["steps"]) == 7
    
    # Assert missing internal fields at root
    for field in ["ReviewType", "DecidedBy", "decision_maker", "manager_note", "agent"]:
        assert field not in c_data
        
    # Assert missing internal fields in steps
    for step in c_data["steps"]:
        for field in ["ReviewType", "DecidedBy", "decision_maker", "manager_note", "agent", "clause", "cited_clause"]:
            assert field not in step
            
    # Check generic Step 7
    assert c_data["steps"][6]["name"] == "Manager confirmation to release funds"
