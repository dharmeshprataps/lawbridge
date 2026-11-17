import Lean

/-! A signed journal model for a split-expense ledger. Amounts are exact integers.
The model proves conservation for all finite histories of balanced events;
conformance of Python HTTP executions is checked separately. -/
namespace Lawbridge.SplitExpenseModel

structure Posting where
  memberId : String
  delta : Int
  deriving Repr, DecidableEq, BEq

structure Share where
  memberId : String
  amount : Int
  deriving Repr, DecidableEq, BEq

def total : List Posting → Int
  | [] => 0
  | posting :: rest => posting.delta + total rest

def shareTotal : List Share → Int
  | [] => 0
  | share :: rest => share.amount + shareTotal rest

def debits : List Share → List Posting
  | [] => []
  | share :: rest => ⟨share.memberId, -share.amount⟩ :: debits rest

def expensePostings (payer : String) (amount : Int) (shares : List Share) : List Posting :=
  ⟨payer, amount⟩ :: debits shares

def settlementPostings (sender receiver : String) (amount : Int) : List Posting :=
  [⟨sender, amount⟩, ⟨receiver, -amount⟩]

def memberBalance (memberId : String) : List Posting → Int
  | [] => 0
  | posting :: rest =>
    (if posting.memberId == memberId then posting.delta else 0) + memberBalance memberId rest

theorem debits_total (shares : List Share) : total (debits shares) = -shareTotal shares := by
  induction shares with
  | nil => rfl
  | cons head tail ih =>
    simp only [debits, total, shareTotal]
    omega

theorem expense_balanced (payer : String) (amount : Int) (shares : List Share)
    (hShares : shareTotal shares = amount) :
    total (expensePostings payer amount shares) = 0 := by
  simp only [expensePostings, total, debits_total]
  omega

theorem settlement_balanced (sender receiver : String) (amount : Int) :
    total (settlementPostings sender receiver amount) = 0 := by
  simp only [settlementPostings, total]
  omega

theorem append_total (before event : List Posting) :
    total (before ++ event) = total before + total event := by
  induction before with
  | nil => simp [total]
  | cons head tail ih =>
    simp only [List.cons_append, total, ih]
    omega

theorem balanced_event_preserves_total (before event : List Posting)
    (hEvent : total event = 0) : total (before ++ event) = total before := by
  rw [append_total, hEvent]
  omega

theorem member_balance_append (memberId : String) (before event : List Posting) :
    memberBalance memberId (before ++ event) =
      memberBalance memberId before + memberBalance memberId event := by
  induction before with
  | nil => simp [memberBalance]
  | cons head tail ih =>
    simp only [List.cons_append, memberBalance, ih]
    omega

inductive BalancedHistory : List Posting → List Posting → Prop where
  | empty (state : List Posting) : BalancedHistory state state
  | step {initial before : List Posting} (event : List Posting) :
      BalancedHistory initial before → total event = 0 →
      BalancedHistory initial (before ++ event)

theorem history_preserves_total (initial final : List Posting)
    (hHistory : BalancedHistory initial final) : total final = total initial := by
  induction hHistory with
  | empty => rfl
  | step event _ hEvent ih =>
    rw [balanced_event_preserves_total _ _ hEvent]
    exact ih

theorem history_preserves_zero (initial final : List Posting)
    (hInitial : total initial = 0) (hHistory : BalancedHistory initial final) :
    total final = 0 := by
  rw [history_preserves_total initial final hHistory, hInitial]

#print axioms debits_total
#print axioms expense_balanced
#print axioms settlement_balanced
#print axioms append_total
#print axioms balanced_event_preserves_total
#print axioms member_balance_append
#print axioms history_preserves_total
#print axioms history_preserves_zero

end Lawbridge.SplitExpenseModel
