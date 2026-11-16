# Board page fields

Read this before you `dashboard_write` a board page's `needs_you` or `asks`
field, or call `panel_publish`. The `[DASHBOARD]` block on your turn names the
page you have.

## The `kirocrew-conductor` page

A Bot named `kirocrew-conductor` that adopted no other page gets the `kirocrew-conductor` page in its Dashboard tab; the `[DASHBOARD]` block names the page you have. That page draws every task from your work ledger. A needs-you item goes into its agentic field `needs_you` with `dashboard_write`: one short line per thing the user must do, in the user's language; drop a line once it is done. `panel_publish` (template `kirocrew-conductor`) shows only on a page that reads it; this page shows only its `next`, so needs-you items go in `needs_you`. It takes `{needs_you, done: "N of M", updated, next, repo?: "owner/name", tasks: [{task, state, step, pr?}]}` (with `repo`, a task's `pr` opens its GitHub PR) -- state one of done/working/testing/waiting/needs you/stuck, step one of Code/Test/PR/CI/Done (`kiro_crew.conductor_board_contract`).

## The `goal-board` page

On the `goal-board` page only: it gives every needs-you item a Reply button. To offer answers too, `dashboard_write` its one agentic field `asks`: `{<item_id>: {options: ["short answer", ...]}}`, in the user's language, each a full answer that reads on its own beside the worker's report. Each option becomes a button that fills the user's chat box; drop an item's entry once it is answered. On your own `kirocrew-conductor` page an `asks` write is refused; write `needs_you` there instead.
