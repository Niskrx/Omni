# Section I: Standard Loan Routing
- **Clause 1**: Standard loan routing follows a strict serial flow (A -> B -> C). Subsidy loans carry additional compliance rules. A verified guarantor can offset a primary risk flag. Disbursement-splitting rules must be evaluated prior to final approval.
- **Clause 2**: Under emergency or disaster bypass conditions, routing flows directly from Agent A to Agent C for expedited execution, while Agent B monitors asynchronously.
- **Clause 4**: Autonomous execution is strictly limited to an explicit threshold of ₹50,000.
- **Clause 5**: ANY transaction touching a frozen or locked fund requires immediate human review, regardless of the transaction amount.
- **Clause 6**: Proportionality Review. A requested amount within the ₹50,000 autonomous threshold must still be flagged for human review if disproportionate to declared annual income. Proportionality Ratio (PR) = Requested Amount / Declared Annual Income. PR > 0.30 triggers review under this clause. This is a deliberate delegation of a soft judgment call to a person, not a policy violation.

# Section II: Negotiation Constraints
- **Clause 1**: Automated negotiations are capped at a maximum of three (3) rounds.
- **Clause 2**: A "structural change" (e.g., changes to term length, collateral type, or interest rate tier) forces human review, even if the proposed amount is under the autonomous threshold.

# Section III: Audit Requirements
- **Clause 1**: Every transaction must be logged with the following fields: TransactionID, AgentsInvolved, Route, Outcome, Timestamp, and PolicyVersion.

# Section IV: Fund Release Controls
- **Clause 1**: Maker-Checker Principle: no funds disbursed without explicit manager release action, regardless of whether the case was auto-eligible or flagged. Autonomous eligibility changes whether that action is a routine confirmation or a substantive judgment — never whether the action is required.
- **Clause 2**: Attribution: every resolved transaction records whether the outcome is attributable to policy match (agents cleared, manager only confirmed release) or manager judgment (manager made a substantive decision, including overriding an agent clearance). Must drive different customer-facing language.
