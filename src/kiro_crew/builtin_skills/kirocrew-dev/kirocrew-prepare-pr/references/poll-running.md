# `pr_status.py` exit 10: arming the poll loop

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (Phase 3 step 5). The call itself is in `monitor-loop.md`.

   - **10** → still running. In a chat slot, load `kirocrew-core::monitor_start`
     as the `[TOOL NAMES]` block says, request a finite same-session loop, then END THE TURN.
     Slot-less subagent, cron, webhook and task-runner turns cannot arm one: use
     bounded in-turn `wait` + re-poll and disclose that fallback.

     Arm it with the call in `references/monitor-loop.md` (read it first, swap in the
     real PR URL): `interval_secs=600, max_cycles=144, max_runtime_secs=86400,
     judge=False`. The loop is gated: an unchanged PR costs no turn and any change
     wakes it. Under a merge hold, rewrite its `Exit 0` clause as that file says; a
     hold heard after arming goes in with `monitor_update(message=...)` that turn
     (`references/rationale.md`). Omit `banner` on Slack/Discord/Webex. Loop mechanics
     belong to the `babysit` skill.

     A create-only refusal means a loop may already be active: inspect it on a
     later turn; never start another driver beside it. Missing hosting context
     permits bounded wait/poll. A retained-stop refusal needs the owner, not a
     retry. A manual pause or user stop is preserved; only an authorized budget
     increase revives a budget-paused loop.

     An acknowledgement is only a pending request. END THE TURN so it can apply;
     do not retry because no loop is visible before the turn ends. On a later
     turn verify it with `monitor_inspect()` or
     `python3 $SKILL_DIR/scripts/monitor_armed.py --pr <n>` (**0** armed, **20**
     not, **2** unreadable — not proof of absence), and confirm `cycle_count`
     advances. Keep pending cycles to one status poll.
