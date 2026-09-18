---
name: goal-ledger-conductor
description: Deprecated alias of the goal-conductor skill, removed next release. The work-ledger conducting procedure - decompose a goal into items, bind each to a session, read reported status as data, verify claims with the acceptance evaluator - now lives in goal-conductor. Read that skill instead of this one.
---

# Goal Ledger Conductor (deprecated)

This skill's procedure is now `goal-conductor`, and this name stays for one
release only. `kirocrew-conductor` IS the ledger conductor: it mounts
`@kirocrew-work`, dispatches with bind-before-seed, and settles every `done`
claim with `scripts/accept_eval.py` — so the split this skill existed to hold
open is closed. Read `goal-conductor` and follow it; nothing new should name
this skill. The bundled `scripts/accept_eval.py` here is a byte-identical copy
of `goal-conductor`'s, kept so a session that already loaded this name keeps a
working evaluator until the name is removed.
