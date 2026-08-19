/-
Formal model of the coordination-thrash report (spec T1/T2/T5,
.erpaval/specs/001-routing-thrash).

Models the derived read over a goal's event history: the report is a fold over
recorded signals, so T2 (empty history → zeros) and T5 (counters nondecreasing
under history extension) hold by construction. T1's goal-scoping is modeled as
a frame property: events for other goals never change the report.

Mirrors the planned `application/use_cases/thrash_service.py`: the Python
implementation computes the same counters with SQL COUNT(*) FILTER clauses;
these theorems pin the semantics those queries must satisfy.
-/

namespace Blackboard

/-- The thrash-relevant signal classes extracted from a goal's history
    (team_events + processed_commands error codes). -/
inductive Signal
  | conflict        -- CommandResult error_code = CONFLICT (double-claim, dup review, CAS races)
  | staleVersion    -- error_code = STALE_VERSION (optimistic-concurrency misses)
  | reviewRejected  -- review submitted with disposition != approved
  | reclaim         -- task re-entered READY after a completed/failed assignment
  | other           -- any other event; must never move a counter
  deriving DecidableEq, Repr

/-- One recorded item: which goal it belongs to and its signal class. -/
structure Event (γ : Type) where
  goal : γ
  signal : Signal

/-- The report: four counters (spec T1). -/
structure ThrashReport where
  conflicts : Nat
  staleVersions : Nat
  reviewRejections : Nat
  reclaims : Nat
  deriving DecidableEq, Repr

def ThrashReport.zero : ThrashReport := ⟨0, 0, 0, 0⟩

/-- Counter-wise ≤ on reports. -/
def ThrashReport.le (a b : ThrashReport) : Prop :=
  a.conflicts ≤ b.conflicts ∧ a.staleVersions ≤ b.staleVersions ∧
  a.reviewRejections ≤ b.reviewRejections ∧ a.reclaims ≤ b.reclaims

instance : LE ThrashReport := ⟨ThrashReport.le⟩

/-- Fold one signal into the report. `other` is the identity — the frame case. -/
def step (r : ThrashReport) : Signal → ThrashReport
  | .conflict       => { r with conflicts := r.conflicts + 1 }
  | .staleVersion   => { r with staleVersions := r.staleVersions + 1 }
  | .reviewRejected => { r with reviewRejections := r.reviewRejections + 1 }
  | .reclaim        => { r with reclaims := r.reclaims + 1 }
  | .other          => r

/-- The report for goal `g` over a history: fold over exactly g's events (T1). -/
def report {γ : Type} [DecidableEq γ] (g : γ) (history : List (Event γ)) : ThrashReport :=
  (history.filter (fun e => e.goal == g)).foldl (fun r e => step r e.signal) ThrashReport.zero

/-- T2: empty history yields the zero report, not an error. -/
theorem report_empty {γ : Type} [DecidableEq γ] (g : γ) :
    report g ([] : List (Event γ)) = ThrashReport.zero := rfl

/-- A step never decreases any counter. -/
theorem step_mono (r : ThrashReport) (s : Signal) : r ≤ step r s := by
  cases s <;> exact ⟨by simp [step], by simp [step], by simp [step], by simp [step]⟩

/-- ≤ is transitive on reports. -/
theorem le_trans' (a b c : ThrashReport) : a ≤ b → b ≤ c → a ≤ c := by
  intro hab hbc
  exact ⟨Nat.le_trans hab.1 hbc.1, Nat.le_trans hab.2.1 hbc.2.1,
         Nat.le_trans hab.2.2.1 hbc.2.2.1, Nat.le_trans hab.2.2.2 hbc.2.2.2⟩

theorem le_refl' (a : ThrashReport) : a ≤ a :=
  ⟨Nat.le_refl _, Nat.le_refl _, Nat.le_refl _, Nat.le_refl _⟩

/-- Folding events from a starting report never decreases it. -/
theorem foldl_events_mono {γ : Type} (r : ThrashReport) (es : List (Event γ)) :
    r ≤ es.foldl (fun r e => step r e.signal) r := by
  induction es generalizing r with
  | nil => exact le_refl' r
  | cons e rest ih =>
      exact le_trans' _ _ _ (step_mono r e.signal) (ih (step r e.signal))

/-- T5: monotonicity — extending the history never decreases any counter. -/
theorem report_mono {γ : Type} [DecidableEq γ] (g : γ)
    (history ext : List (Event γ)) :
    report g history ≤ report g (history ++ ext) := by
  unfold report
  rw [List.filter_append, List.foldl_append]
  exact foldl_events_mono _ _

/-- T1 frame property: events for a different goal never change the report. -/
theorem report_frame {γ : Type} [DecidableEq γ] (g : γ)
    (history : List (Event γ)) (e : Event γ) (hne : e.goal ≠ g) :
    report g (history ++ [e]) = report g history := by
  unfold report
  rw [List.filter_append]
  have hbeq : (e.goal == g) = false := beq_eq_false_iff_ne.mpr hne
  have : List.filter (fun x => x.goal == g) [e] = [] := by
    simp [List.filter, hbeq]
  rw [this, List.append_nil]

/-- `other`-class events never move a counter even for the right goal. -/
theorem report_other_id {γ : Type} [DecidableEq γ] (g : γ)
    (history : List (Event γ)) :
    report g (history ++ [⟨g, .other⟩]) = report g history := by
  unfold report
  rw [List.filter_append, List.foldl_append]
  simp [List.filter, step]

end Blackboard
