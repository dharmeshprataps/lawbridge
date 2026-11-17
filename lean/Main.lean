import Lawbridge.Checker
import Lawbridge.AccountModel

private partial def run (stdin stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  stdout.putStrLn (Lawbridge.handleLine line).compress
  stdout.flush
  run stdin stdout

def main : IO Unit := do
  run (← IO.getStdin) (← IO.getStdout)
