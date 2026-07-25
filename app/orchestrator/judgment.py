from decimal import Decimal

def calculate_household_adjustment_factor(married: bool, dependents: int) -> float:
    """
    1 - (0.05 * dependents) - (0.05 if married else 0), floor at 0.6
    """
    factor = 1.0 - (0.05 * dependents) - (0.05 if married else 0.0)
    return max(0.6, factor)

def calculate_proportionality_ratio(loan_amount: float | Decimal, annual_income: float | Decimal, haf: float) -> float:
    """
    loan_amount / (annual_income * haf); raise ValueError if annual_income <= 0
    """
    loan_amount_float = float(loan_amount)
    annual_income_float = float(annual_income)
    
    if annual_income_float <= 0:
        raise ValueError("Annual income must be greater than 0")
        
    adjusted_income = annual_income_float * haf
    if adjusted_income <= 0:
        raise ValueError("Adjusted income must be greater than 0")
        
    return loan_amount_float / adjusted_income

def is_disproportionate(ratio: float) -> bool:
    """
    ratio > 0.30
    """
    return ratio > 0.30

def is_credit_score_flagged(credit_score: int) -> bool:
    """
    credit_score < 650
    """
    return credit_score < 650
