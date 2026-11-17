import Lake
open Lake DSL

package lawbridge where

lean_lib Lawbridge where

@[default_target]
lean_exe lawcheck where
  root := `Main
