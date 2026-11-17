import Lean

/-!
An independently specified account model. These are proofs about this model,
not proofs that an arbitrary Python implementation conforms to the model.
-/

namespace Lawbridge.AccountModel

structure Account where
  balance : Int
  spent : Int
  limit : Int
  owner : String
  deriving Repr, DecidableEq

def Valid (s : Account) : Prop :=
  0 ≤ s.balance ∧ 0 ≤ s.spent ∧ s.spent ≤ s.limit

def initialAccount (balance limit : Int) (owner : String) : Account :=
  { balance := balance, spent := 0, limit := limit, owner := owner }

def Admissible (s : Account) (amount : Int) : Prop :=
  0 ≤ amount ∧ amount ≤ s.balance ∧ s.spent + amount ≤ s.limit

def withdraw (s : Account) (amount : Int) : Account :=
  { s with balance := s.balance - amount, spent := s.spent + amount }

def AllowedWithdrawal (before after : Account) (amount : Int) : Prop :=
  Admissible before amount ∧ after = withdraw before amount

instance (s : Account) : Decidable (Valid s) := by
  unfold Valid
  infer_instance

instance (s : Account) (amount : Int) : Decidable (Admissible s amount) := by
  unfold Admissible
  infer_instance

instance (before after : Account) (amount : Int) :
    Decidable (AllowedWithdrawal before after amount) := by
  unfold AllowedWithdrawal
  infer_instance

theorem initialize_valid (balance limit : Int) (owner : String)
    (hBalance : 0 ≤ balance) (hLimit : 0 ≤ limit) :
    Valid (initialAccount balance limit owner) := by
  simp only [Valid, initialAccount]
  exact ⟨hBalance, by omega, hLimit⟩

theorem withdrawal_preserves_validity (before after : Account) (amount : Int)
    (hValid : Valid before) (hAllowed : AllowedWithdrawal before after amount) :
    Valid after := by
  rcases hValid with ⟨hBalance, hSpent, hLimit⟩
  rcases hAllowed with ⟨⟨hAmount, hAffordable, hWithinLimit⟩, hAfter⟩
  subst after
  simp only [Valid, withdraw]
  exact ⟨by omega, by omega, hWithinLimit⟩

theorem withdrawal_preserves_owner (before after : Account) (amount : Int)
    (hAllowed : AllowedWithdrawal before after amount) :
    after.owner = before.owner := by
  rcases hAllowed with ⟨_, hAfter⟩
  subst after
  rfl

theorem withdrawal_preserves_limit (before after : Account) (amount : Int)
    (hAllowed : AllowedWithdrawal before after amount) :
    after.limit = before.limit := by
  rcases hAllowed with ⟨_, hAfter⟩
  subst after
  rfl

theorem withdrawal_conserves_accounting (before after : Account) (amount : Int)
    (hAllowed : AllowedWithdrawal before after amount) :
    after.balance + after.spent = before.balance + before.spent := by
  rcases hAllowed with ⟨_, hAfter⟩
  subst after
  simp only [withdraw]
  omega

inductive WithdrawalTrace : Account → Account → Prop where
  | empty (state : Account) : WithdrawalTrace state state
  | step {initial before after : Account} (amount : Int) :
      WithdrawalTrace initial before →
      AllowedWithdrawal before after amount →
      WithdrawalTrace initial after

theorem trace_preserves_validity (initial final : Account)
    (hInitial : Valid initial) (hTrace : WithdrawalTrace initial final) :
    Valid final := by
  induction hTrace with
  | empty => exact hInitial
  | step amount _ hAllowed ih =>
    exact withdrawal_preserves_validity _ _ amount ih hAllowed

-- Build automation audits these dependencies. No sorryAx or custom axioms are
-- permitted. Standard Lean logical axioms, when reported, are explicit.
#print axioms initialize_valid
#print axioms withdrawal_preserves_validity
#print axioms withdrawal_preserves_owner
#print axioms withdrawal_preserves_limit
#print axioms withdrawal_conserves_accounting
#print axioms trace_preserves_validity

end Lawbridge.AccountModel
