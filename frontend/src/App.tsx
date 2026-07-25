import React, { useState, useEffect } from 'react';
import { CheckCircle2, Circle, ChevronRight, FileText } from 'lucide-react';

type Step = {
  step_num: number;
  name: string;
  status: string;
};

type StatusResponse = {
  transaction_id: string;
  status: string;
  final_message: string | null;
  steps: Step[];
  required_documents: string[];
};

export default function App() {
  const [transactionId, setTransactionId] = useState<string | null>(null);
  const [statusData, setStatusData] = useState<StatusResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Form states
  const [fullName, setFullName] = useState('');
  const [loanAmount, setLoanAmount] = useState('');
  const [income, setIncome] = useState('');
  const [creditScore, setCreditScore] = useState('700');
  const [maritalStatus, setMaritalStatus] = useState('Single');
  const [dependents, setDependents] = useState('0');
  const [loanType, setLoanType] = useState('standard_loan');
  const [subsidyPct, setSubsidyPct] = useState('');
  const [guarantor, setGuarantor] = useState('');
  const [email, setEmail] = useState('');

  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;
    if (transactionId && statusData?.status !== 'approved' && statusData?.status !== 'rejected') {
      interval = setInterval(async () => {
        try {
          const res = await fetch(`/api/v1/applications/${transactionId}/customer-status`);
          if (res.ok) {
            const data = await res.json();
            setStatusData(data);
          }
        } catch (e) {
          console.error(e);
        }
      }, 1500);
    }
    return () => clearInterval(interval);
  }, [transactionId, statusData?.status]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const payload = {
        customer_name: fullName,
        loan_type: loanType,
        requested_amount: parseFloat(loanAmount),
        annual_declared_income: parseFloat(income),
        is_urgent: loanType === 'emergency_payout',
        target_fund: loanType === 'emergency_payout' ? 'Disaster Relief Fund' : 'General Subsidy Pool',
        credit_score: parseInt(creditScore, 10),
        marital_status: maritalStatus,
        dependents: parseInt(dependents, 10),
        metadata: {
          subsidy_pct: subsidyPct,
          guarantor: guarantor,
          email: email,
        },
      };
      
      const res = await fetch('/api/v1/applications', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      setTransactionId(data.transaction_id);
    } catch (e) {
      console.error(e);
    }
    setIsSubmitting(false);
  };

  const isComplete = statusData?.status === 'approved' || statusData?.status === 'rejected';

  if (transactionId && statusData) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
        <div className="max-w-2xl w-full bg-white rounded-xl shadow-lg p-8">
          <h1 className="text-2xl font-bold text-gray-900 mb-6">Application Status</h1>
          <div className="mb-4 text-sm text-gray-500">Transaction ID: {transactionId}</div>
          
          {!isComplete && (
            <div className="space-y-4 mt-8">
              {statusData.steps.map((step) => (
                <div key={step.step_num} className="flex items-center space-x-3">
                  {step.status === 'completed' ? (
                    <CheckCircle2 className="w-5 h-5 text-emerald-500" />
                  ) : step.status === 'pending' ? (
                    <div className="w-5 h-5 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />
                  ) : (
                    <Circle className="w-5 h-5 text-gray-300" />
                  )}
                  <span className={`text-sm font-medium ${step.status === 'completed' ? 'text-gray-900' : 'text-gray-500'}`}>
                    {step.name}
                  </span>
                </div>
              ))}
            </div>
          )}

          {isComplete && statusData.final_message && (
            <div className={`mt-6 p-4 rounded-lg border ${statusData.status === 'approved' ? 'bg-emerald-50 border-emerald-200' : 'bg-red-50 border-red-200'}`}>
              <p className={`text-sm font-medium ${statusData.status === 'approved' ? 'text-emerald-800' : 'text-red-800'}`}>
                {statusData.final_message}
              </p>
            </div>
          )}

          {isComplete && statusData.required_documents && statusData.required_documents.length > 0 && (
            <div className="mt-8 pt-6 border-t border-gray-100">
              <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2 mb-3">
                <FileText className="w-4 h-4 text-blue-500" />
                Documents You'll Need
              </h3>
              <ul className="space-y-2">
                {statusData.required_documents.map((doc, i) => (
                  <li key={i} className="text-sm text-gray-600 flex items-center gap-2">
                    <ChevronRight className="w-4 h-4 text-gray-400" />
                    {doc}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {isComplete && (
            <button 
              onClick={() => {
                setTransactionId(null);
                setStatusData(null);
              }}
              className="mt-8 w-full bg-blue-600 text-white rounded-lg px-4 py-2 hover:bg-blue-700 transition-colors"
            >
              Start New Application
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-xl mx-auto bg-white rounded-xl shadow-lg p-8">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-gray-900 tracking-tight">Apply for Funds</h1>
          <p className="mt-2 text-sm text-gray-500">Fast, automated eligibility checks.</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Full Name</label>
            <input required type="text" value={fullName} onChange={e => setFullName(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Loan Amount (₹)</label>
              <input required type="number" value={loanAmount} onChange={e => setLoanAmount(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Declared Annual Income (₹)</label>
              <input required type="number" value={income} onChange={e => setIncome(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Credit Score</label>
              <input required type="number" min="300" max="900" value={creditScore} onChange={e => setCreditScore(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Marital Status</label>
              <select value={maritalStatus} onChange={e => setMaritalStatus(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500">
                <option value="Single">Single</option>
                <option value="Married">Married</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Dependents</label>
              <input required type="number" min="0" value={dependents} onChange={e => setDependents(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Loan Type</label>
            <select value={loanType} onChange={e => setLoanType(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500">
              <option value="standard_loan">Standard / Subsidy Loan</option>
              <option value="emergency_payout">Emergency Payout</option>
            </select>
          </div>

          {loanType === 'standard_loan' && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Requested Subsidy % (Optional)</label>
              <input type="number" min="0" max="100" value={subsidyPct} onChange={e => setSubsidyPct(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Guarantor Name (Optional)</label>
            <input type="text" value={guarantor} onChange={e => setGuarantor(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Email (Optional)</label>
            <input type="email" placeholder="Want a confirmation email?" value={email} onChange={e => setEmail(e.target.value)} className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500" />
          </div>

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full bg-blue-600 text-white rounded-lg px-4 py-3 font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {isSubmitting ? (
              <div className="w-5 h-5 rounded-full border-2 border-white/30 border-t-white animate-spin" />
            ) : null}
            Check My Eligibility
          </button>
        </form>
      </div>
    </div>
  );
}
