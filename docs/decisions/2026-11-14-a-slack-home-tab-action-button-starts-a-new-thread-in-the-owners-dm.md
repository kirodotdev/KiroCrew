# A Slack Home tab action button starts a new thread in the owner's DM with the bot

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14

## Decision

A click on a button in the bot's Slack Home tab whose `action_id` starts with
`options_choice_`, whose suffix after that prefix is not only digits, which does
not contain `_done_`, and whose `value` starts with `action::` posts the button's
label as a new top-level message in the owner's DM with the bot and runs the
agent turn there exactly as a card's action button does. Only the owner's click
counts, and it then passes the channel checks a click in that DM passes; the Home
view is never edited, and this button shape is a contract for owner-built Home
views.

## Why

- A Home tab button did nothing: the click carries no channel and no message, so
  the dispatcher had nowhere to answer it.
- The owner's Home tab buttons reach the agent in their DM, which is where the
  Home tab's Resume button already sends them.
- It applies whether or not the built-in Home page is published; that page has
  no such button.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36246#issuecomment-6876081118 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/36246 -- the issue.
