import Lean
import Lawbridge.SplitExpenseModel

namespace Lawbridge.SplitExpenseChecker
open Lean SplitExpenseModel

private def field (object : Json) (name : String) : Except String Json := object.getObjVal? name
private def str (object : Json) (name : String) : Except String String := do
  (← field object name).getStr?
private def int (object : Json) (name : String) : Except String Int := do
  (← field object name).getInt?
private def arr (object : Json) (name : String) : Except String (Array Json) := do
  (← field object name).getArr?

private def shape (object : Json) (keys : List String) : Except String Unit := do
  let fields ← object.getObj?
  let actual := fields.foldl (init := []) (fun names key _ => key :: names)
  unless actual.length == keys.length && actual.all keys.contains do
    throw s!"expected exactly the fields {keys}"

private def boundedString (value : String) (maximum : Nat := 200) : Bool :=
  !value.trimAscii.toString.isEmpty && value.length ≤ maximum

private def checkedString (object : Json) (name : String) (maximum : Nat := 200) :
    Except String String := do
  let value ← str object name
  unless boundedString value maximum do throw s!"invalid string field {name}"
  pure value

private def distinct (values : List String) : Bool :=
  values.eraseDups.length == values.length

private def parseShare (value : Json) : Except String Share := do
  shape value ["member_id", "amount_minor"]
  pure ⟨← checkedString value "member_id", ← int value "amount_minor"⟩

private def parsePosting (value : Json) : Except String Posting := do
  shape value ["member_id", "delta_minor"]
  pure ⟨← checkedString value "member_id", ← int value "delta_minor"⟩

private def postingJson (posting : Posting) : Json :=
  Json.mkObj [("member_id", .str posting.memberId), ("delta_minor", toJson posting.delta)]

private def shareJson (share : Share) : Json :=
  Json.mkObj [("member_id", .str share.memberId), ("amount_minor", toJson share.amount)]

private structure Snapshot where
  raw : Json
  members : List String
  journal : List Posting
  expenses : Array Json
  settlements : Array Json
  requests : Array Json

private def parseSnapshot (value : Json) : Except String Snapshot := do
  shape value ["id", "name", "currency", "members", "balances", "expenses", "settlements", "journal", "requests"]
  let _ ← checkedString value "id"
  let _ ← checkedString value "name"
  let currency ← str value "currency"
  unless currency.length == 3 && currency.toList.all (fun c => 'A' ≤ c && c ≤ 'Z') do
    throw "snapshot currency must be three uppercase ASCII letters"
  let membersJson ← arr value "members"
  unless 2 ≤ membersJson.size && membersJson.size ≤ 50 do throw "snapshot must have 2..50 members"
  let members ← membersJson.toList.mapM fun member => do
    shape member ["id", "name"]
    let _ ← checkedString member "name"
    checkedString member "id"
  unless distinct members do throw "snapshot member IDs must be unique"
  let journal ← (← arr value "journal").toList.mapM parsePosting
  unless journal.all (fun posting => members.contains posting.memberId) do
    throw "journal refers to a nonmember"
  unless total journal == 0 do throw "journal total is not zero"
  let balances ← arr value "balances"
  unless balances.size == members.length do throw "balance list does not cover every member"
  for index in [:balances.size] do
    let balance := balances[index]!
    shape balance ["member_id", "amount_minor"]
    let memberId ← str balance "member_id"
    unless memberId == members[index]! do throw "balances must be in member order"
    unless (← int balance "amount_minor") == memberBalance memberId journal do
      throw s!"balance for {memberId} disagrees with the journal"
  let expenses ← arr value "expenses"
  let settlements ← arr value "settlements"
  let requests ← arr value "requests"
  unless requests.size == expenses.size + settlements.size do
    throw "request count does not match committed events"
  let mut keys : List String := []
  for request in requests do
    shape request ["kind", "key", "body", "response"]
    let kind ← str request "kind"
    unless kind == "expense" || kind == "settlement" do throw "unknown request kind"
    let key ← checkedString request "key" 128
    let scopedKey := kind ++ ":" ++ key
    unless !keys.contains scopedKey do throw "duplicate scoped idempotency key"
    keys := scopedKey :: keys
    let _ ← (← field request "body").getObj?
    let response ← field request "response"
    let events := if kind == "expense" then expenses else settlements
    unless events.contains response do throw "request response is not a committed event"
  pure ⟨value, members, journal, expenses, settlements, requests⟩

private inductive Split where
  | equal (members : List String)
  | exact (shares : List Share)

private inductive Operation where
  | expense (key description payer : String) (amount : Int) (split : Split)
  | settlement (key sender receiver : String) (amount : Int)

private def Operation.key : Operation → String
  | .expense key _ _ _ _ => key
  | .settlement key _ _ _ => key

private def parseOperation (kind : String) (body : Json) : Except String Operation := do
  if kind == "expense" then
    shape body ["idempotency_key", "description", "payer_id", "amount_minor", "split"]
    let key ← checkedString body "idempotency_key" 128
    let description ← checkedString body "description"
    let payer ← checkedString body "payer_id"
    let amount ← int body "amount_minor"
    let splitJson ← field body "split"
    let splitKind ← str splitJson "type"
    let split ← if splitKind == "equal" then do
      shape splitJson ["type", "member_ids"]
      let idsJson ← arr splitJson "member_ids"
      let ids ← idsJson.toList.mapM fun value => do
        let id ← value.getStr?
        unless boundedString id do throw "invalid participant ID"
        pure id
      pure (Split.equal ids)
    else if splitKind == "exact" then do
      shape splitJson ["type", "shares"]
      let sharesJson ← arr splitJson "shares"
      pure (Split.exact (← sharesJson.toList.mapM parseShare))
    else throw "unknown split type"
    pure (.expense key description payer amount split)
  else if kind == "settlement" then
    shape body ["idempotency_key", "from_member", "to_member", "amount_minor"]
    pure (.settlement (← checkedString body "idempotency_key" 128)
      (← checkedString body "from_member") (← checkedString body "to_member")
      (← int body "amount_minor"))
  else throw "unsupported operation kind"

private def equalShares (members : List String) (amount : Int) : List Share :=
  let divisor := Int.ofNat members.length
  let quotient := amount / divisor
  let remainder := (amount % divisor).toNat
  members.zipIdx.map fun (memberId, index) =>
    ⟨memberId, quotient + if index < remainder then 1 else 0⟩

private def normalize (before : Snapshot) (operation : Operation) :
    Except String (List Share × List Posting) := do
  match operation with
  | .expense _ _ payer amount split =>
    unless 0 < amount && amount ≤ 1000000000000 do throw "expense amount outside accepted range"
    unless before.members.contains payer do throw "payer is not a member"
    let shares ← match split with
      | .equal members => do
        unless 1 ≤ members.length && members.length ≤ 50 do throw "split must have 1..50 participants"
        pure (equalShares members amount)
      | .exact shares => do
        unless 1 ≤ shares.length && shares.length ≤ 50 do throw "split must have 1..50 shares"
        pure shares
    let participants := shares.map (·.memberId)
    unless distinct participants && participants.all before.members.contains do
      throw "participants must be distinct members"
    unless shares.all (fun share => 0 ≤ share.amount && share.amount ≤ 1000000000000) do
      throw "share amount outside accepted range"
    unless shareTotal shares == amount do throw "shares do not sum to the expense amount"
    pure (shares, expensePostings payer amount shares)
  | .settlement _ sender receiver amount =>
    unless 0 < amount && amount ≤ 1000000000000 do throw "settlement amount outside accepted range"
    unless sender != receiver && before.members.contains sender && before.members.contains receiver do
      throw "settlement parties must be distinct members"
    let senderBalance := memberBalance sender before.journal
    let receiverBalance := memberBalance receiver before.journal
    unless senderBalance < 0 && 0 < receiverBalance && amount ≤ -senderBalance && amount ≤ receiverBalance do
      throw "settlement exceeds outstanding balances or has the wrong direction"
    pure ([], settlementPostings sender receiver amount)

private structure Expected where
  status : Int
  eligible : Bool
  operation : Option Operation := none
  shares : List Share := []
  postings : List Posting := []
  replayValue : Option Json := none

private def expectedOutcome (kind : String) (body : Json) (before : Snapshot) :
    Except String Expected := do
  let operation ← match parseOperation kind body with
    | .error _ => return { status := 400, eligible := false }
    | .ok operation => pure operation
  let mut existing : Option Json := none
  for request in before.requests do
    if (← str request "kind") == kind && (← str request "key") == operation.key then
      existing := some request
  match existing with
  | some request =>
    if (← field request "body") == body then
      let original ← field request "response"
      pure { status := 200, eligible := true, replayValue := some original }
    else pure { status := 409, eligible := false }
  | none =>
    match normalize before operation with
    | .error _ => pure { status := 400, eligible := false }
    | .ok (shares, postings) =>
      pure { status := 201, eligible := true, operation := (some operation), shares := shares, postings := postings }

private def expectedResponse (operation : Operation) (shares : List Share) (response : Json) :
    Except String Json := do
  let eventId ← checkedString response "id"
  match operation with
  | .expense key description payer amount _ =>
    shape response ["id", "idempotency_key", "description", "payer_id", "amount_minor", "shares"]
    pure (Json.mkObj [("id", .str eventId), ("idempotency_key", .str key),
      ("description", .str description), ("payer_id", .str payer),
      ("amount_minor", toJson amount), ("shares", .arr (shares.toArray.map shareJson))])
  | .settlement key sender receiver amount =>
    shape response ["id", "idempotency_key", "from_member", "to_member", "amount_minor"]
    pure (Json.mkObj [("id", .str eventId), ("idempotency_key", .str key),
      ("from_member", .str sender), ("to_member", .str receiver), ("amount_minor", toJson amount)])

private def responseJson (id : String) (eligible : Bool) (failures : Array String) : Json :=
  Json.mkObj [("id", .str id), ("passed", .bool failures.isEmpty),
    ("eligible", .bool eligible), ("failures", .arr (failures.map Json.str))]

def checkRequest (request : Json) : Except String Json := do
  let id ← str request "id"
  let record ← field request "record"
  let input ← field record "input"
  let kind ← str input "operation"
  unless kind == "expense" || kind == "settlement" do throw "unsupported split-expense operation"
  let body ← field input "body"
  let before ← match parseSnapshot (← field record "before") with
    | .ok value => pure value
    | .error message => return responseJson id false #["before snapshot: " ++ message]
  let expected ← expectedOutcome kind body before
  let after ← match parseSnapshot (← field record "after") with
    | .ok value => pure value
    | .error message => return responseJson id expected.eligible #["after snapshot: " ++ message]
  let result ← field record "result"
  let status ← int result "status"
  let response ← field result "body"
  let mut failures : Array String := #[]
  unless status == expected.status do
    failures := failures.push s!"HTTP status {status}; expected {expected.status}"
  if expected.status != 201 then
    unless after.raw == before.raw do failures := failures.push "replay or rejected request changed state"
    match expected.replayValue with
    | some original =>
      unless response == original do failures := failures.push "idempotent replay changed the original response"
    | none =>
      let error ← field response "error"
      let _ ← checkedString error "code"
      let _ ← checkedString error "message" 1000
      pure ()
  else
    let operation ← match expected.operation with
      | some value => pure value
      | none => throw "internal missing operation"
    let normalizedResponse ← expectedResponse operation expected.shares response
    unless response == normalizedResponse do failures := failures.push "response does not describe the requested event"
    for name in ["id", "name", "currency", "members"] do
      unless (← field after.raw name) == (← field before.raw name) do
        failures := failures.push s!"group field {name} changed"
    let expectedExpenses := if kind == "expense" then before.expenses.push response else before.expenses
    let expectedSettlements := if kind == "settlement" then before.settlements.push response else before.settlements
    unless after.expenses == expectedExpenses do failures := failures.push "expense records were not appended exactly once"
    unless after.settlements == expectedSettlements do failures := failures.push "settlement records were not appended exactly once"
    let oldEvents := before.expenses ++ before.settlements
    let responseId ← str response "id"
    for event in oldEvents do
      if (← str event "id") == responseId then failures := failures.push "new event reused an existing ID"
    let expectedJournal := before.journal ++ expected.postings
    unless after.journal == expectedJournal do failures := failures.push "journal does not contain exactly the required member postings"
    -- These are the same constructors/total function whose conservation is proved.
    unless total expected.postings == 0 do failures := failures.push "event postings violate the proved conservation model"
    let requestRecord := Json.mkObj [("kind", .str kind), ("key", .str operation.key),
      ("body", body), ("response", response)]
    unless after.requests == before.requests.push requestRecord do
      failures := failures.push "idempotency record was not appended exactly once"
  pure (responseJson id expected.eligible failures)

end Lawbridge.SplitExpenseChecker
