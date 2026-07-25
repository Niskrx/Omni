from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel

class CustomerStep(BaseModel):
    step_num: int
    name: str
    status: str  # "pending", "completed", "error", etc.

class CustomerStatusResponse(BaseModel):
    transaction_id: str
    status: str
    final_message: Optional[str] = None
    steps: List[CustomerStep]
    required_documents: List[str]
