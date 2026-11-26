# A workflow result tab keeps a closed chat's saved title and folder unless the run names them

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

When a workflow result for a closed chat lands in its fallback `workflow-<run_id>` tab, the run's own `title` and `folder` win; where the run gives neither, the tab takes the chat's saved title and folder, and only a conversation with none gets `Workflow: <name>` and stays unfiled.

## Why

- The fallback tab is linked to the chat's transcript, so its saves write that chat's saved line: before this, a run that passed no arguments replaced the person's name and filing with the defaults.
- The `title` argument exists so many runs of one workflow are told apart. A registered webhook hook reuses one session key, so its runs share one saved line; if the saved value won, every later run would show the first run's title.
- A re-minted cron tab already follows the same order: it gets its saved folder back, and the job's own folder replaces it.
- A run that names a `folder` or `title` files or renames the tab, and with it the conversation's saved line, because the caller asked for that and the tab is that conversation.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/42285#issuecomment-7175168160
- https://github.com/kirodotdev/KiroCrew/issues/42285
- https://github.com/kirodotdev/KiroCrew/pull/26019
- https://github.com/kirodotdev/KiroCrew/pull/38848
