import Lean
import Lawbridge.AccountModel
import Lawbridge.SplitExpenseChecker

namespace Lawbridge
open Lean

inductive Value where
  | integer (value : Int)
  | boolean (value : Bool)
  | string (value : String)
  deriving Repr

def getField (object : Json) (name : String) : Except String Json :=
  object.getObjVal? name

def getString (object : Json) (name : String) : Except String String := do
  (← getField object name).getStr?

def getArray (object : Json) (name : String) : Except String (Array Json) := do
  (← getField object name).getArr?

def Value.asInt : Value → Except String Int
  | .integer value => pure value
  | _ => throw "expected an integer (booleans and strings are distinct types)"

def Value.asBool : Value → Except String Bool
  | .boolean value => pure value
  | _ => throw "expected a Boolean law expression"

def valueOfJson : Json → Except String Value
  | value@(.num _) => return .integer (← value.getInt?)
  | .bool value => pure (.boolean value)
  | .str value => pure (.string value)
  | _ => throw "law values must be integers, booleans, or strings"

def pathSegments (expression : Json) : Except String (List String) := do
  let parts ← getArray expression "value"
  let names ← parts.toList.mapM Json.getStr?
  match names with
  | [] => throw "a path must contain at least one segment"
  | root :: _ =>
    unless ["input", "before", "after", "result"].contains root do
      throw "a path must start with input, before, after, or result"
    unless names.all (fun name => !name.isEmpty) do
      throw "path segments must not be empty"
    pure names

def resolvePath (record : Json) (path : List String) : Except String Json :=
  path.foldlM (fun object name => getField object name) record

def isBinaryKind (kind : String) : Bool :=
  ["add", "sub", "mul", "eq", "ne", "lt", "le", "gt", "ge", "and", "or"].contains kind

-- Validate every branch before execution, including branches skipped by
-- Boolean short-circuiting. Fuel bounds both validation and evaluation depth.
def validateExpr : Nat → Json → Except String Unit
  | 0, _ => throw "expression exceeds the supported nesting depth"
  | fuel + 1, expression => do
    let kind ← getString expression "kind"
    match kind with
    | "int" =>
      let _ ← (← getField expression "value").getInt?
      pure ()
    | "bool" =>
      let _ ← (← getField expression "value").getBool?
      pure ()
    | "string" =>
      let _ ← getString expression "value"
      pure ()
    | "path" =>
      let _ ← pathSegments expression
      pure ()
    | "not" | "neg" => validateExpr fuel (← getField expression "arg")
    | _ =>
      unless isBinaryKind kind do throw s!"unsupported expression kind: {kind}"
      validateExpr fuel (← getField expression "left")
      validateExpr fuel (← getField expression "right")

def equalValues (left right : Value) : Except String Bool :=
  match left, right with
  | .integer a, .integer b => pure (a == b)
  | .boolean a, .boolean b => pure (a == b)
  | .string a, .string b => pure (a == b)
  | _, _ => throw "equality requires values of the same supported type"

def evalExpr : Nat → Json → Json → Except String Value
  | 0, _, _ => throw "expression exceeds the supported nesting depth"
  | fuel + 1, expression, record => do
    let kind ← getString expression "kind"
    match kind with
    | "int" => return .integer (← (← getField expression "value").getInt?)
    | "bool" => return .boolean (← (← getField expression "value").getBool?)
    | "string" => return .string (← getString expression "value")
    | "path" => valueOfJson (← resolvePath record (← pathSegments expression))
    | "not" => return .boolean (!(← (← evalExpr fuel (← getField expression "arg") record).asBool))
    | "neg" => return .integer (-(← (← evalExpr fuel (← getField expression "arg") record).asInt))
    | "and" =>
      let left ← (← evalExpr fuel (← getField expression "left") record).asBool
      if !left then return .boolean false
      return .boolean (← (← evalExpr fuel (← getField expression "right") record).asBool)
    | "or" =>
      let left ← (← evalExpr fuel (← getField expression "left") record).asBool
      if left then return .boolean true
      return .boolean (← (← evalExpr fuel (← getField expression "right") record).asBool)
    | _ =>
      let left ← evalExpr fuel (← getField expression "left") record
      let right ← evalExpr fuel (← getField expression "right") record
      match kind with
      | "eq" => return .boolean (← equalValues left right)
      | "ne" => return .boolean (!(← equalValues left right))
      | "add" => return .integer ((← left.asInt) + (← right.asInt))
      | "sub" => return .integer ((← left.asInt) - (← right.asInt))
      | "mul" => return .integer ((← left.asInt) * (← right.asInt))
      | "lt" => return .boolean (decide ((← left.asInt) < (← right.asInt)))
      | "le" => return .boolean (decide ((← left.asInt) ≤ (← right.asInt)))
      | "gt" => return .boolean (decide ((← left.asInt) > (← right.asInt)))
      | "ge" => return .boolean (decide ((← left.asInt) ≥ (← right.asInt)))
      | _ => throw s!"unsupported expression kind: {kind}"

def evalLaw (expression record : Json) : Except String Bool := do
  (← evalExpr 256 expression record).asBool

def stringsJson (values : Array String) : Json :=
  .arr (values.map Json.str)

def resultJson (id : String) (eligible : Bool) (failures : Array String) : Json :=
  Json.mkObj [
    ("id", .str id), ("passed", .bool failures.isEmpty),
    ("eligible", .bool eligible), ("failures", stringsJson failures)]

def parseAccount (value : Json) : Except String AccountModel.Account := do
  pure {
    balance := ← (← getField value "balance").getInt?
    spent := ← (← getField value "spent").getInt?
    limit := ← (← getField value "limit").getInt?
    owner := ← getString value "owner"
  }

def checkModel (model : String) (eligible : Bool) (record : Json) :
    Except String (Array String) := do
  unless model == "account.withdraw.v1" do
    throw s!"unsupported proved model: {model}"
  let before ← parseAccount (← getField record "before")
  let after ← parseAccount (← getField record "after")
  let amount ← (← getField (← getField record "input") "amount").getInt?
  let mut failures : Array String := #[]
  unless decide (AccountModel.Valid before) do
    failures := failures.push "model: before state violates the account invariant"
  let admissible := decide (AccountModel.Admissible before amount)
  if eligible != admissible then
    failures := failures.push "model: contract preconditions disagree with model admissibility"
  if eligible then
    unless decide (AccountModel.AllowedWithdrawal before after amount) do
      failures := failures.push "model: observed transition is not an allowed withdrawal"
  pure failures

def checkRequest (request : Json) : Except String Json := do
  if request.getObjValD "model" == Json.str "splitexpense.v1" then
    return ← SplitExpenseChecker.checkRequest request
  let id ← getString request "id"
  let requires ← getArray request "requires"
  let ensures ← getArray request "ensures"
  if ensures.isEmpty then throw "a contract must contain at least one ensures law"
  for expression in requires ++ ensures do validateExpr 256 expression
  let record ← getField request "record"
  let input ← getField record "input"
  -- Inputs are keyword argument objects; reject other shapes even when a law
  -- does not happen to inspect an argument.
  match input with
  | .obj _ => pure ()
  | _ => throw "record.input must be an object"
  let before ← getField record "before"
  let after ← getField record "after"
  let _ ← getField record "result"
  let error ← getField record "error"
  let ok ← (← getField record "ok").getBool?
  if ok then
    unless error == Json.null do throw "a successful execution must have null error"
  else
    let _ ← getString error "type"
    let _ ← getString error "message"
    pure ()
  let mut eligible := true
  for expression in requires do
    let holds ← evalLaw expression record
    eligible := eligible && holds
  let mut failures : Array String := #[]
  if eligible then
    if !ok then
      failures := failures.push "valid input raised an exception"
    else
      for index in [:ensures.size] do
        unless ← evalLaw ensures[index]! record do
          failures := failures.push s!"ensures[{index}] is false"
  else
    let expected ← getString request "reject_exception"
    if ok then
      failures := failures.push "invalid input was accepted"
    else
      let actual ← getString error "type"
      if actual != expected then
        failures := failures.push s!"invalid input raised {actual}; expected {expected}"
    if before != after then
      failures := failures.push "rejected input changed state"
  match request.getObjVal? "model" with
  | .ok value =>
    let model ← value.getStr?
    failures := failures ++ (← checkModel model eligible record)
  | .error _ => pure ()
  pure (resultJson id eligible failures)

def errorJson (id : Json) (message : String) : Json :=
  Json.mkObj [
    ("id", id), ("passed", .bool false), ("eligible", .bool false),
    ("failures", stringsJson #[message]), ("error", .str message)]

def handleLine (line : String) : Json :=
  match Json.parse line with
  | .error message => errorJson .null s!"invalid JSON: {message}"
  | .ok request =>
    let id := match getString request "id" with
      | .ok value => Json.str value
      | .error _ => Json.null
    match checkRequest request with
    | .ok result => result
    | .error message => errorJson id message

end Lawbridge
