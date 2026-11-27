# The owner's YOLO confirmation acknowledgement is kept on the gateway

Decided by: Bolin Chen (@bolichen97, maintainer)
Date: 2026-11-22

## Decision

The machine owner's *Don't show again* on the YOLO confirmation is also kept on
the gateway, in an owner-only record the agent cannot write (`yolo_ack.json`,
written only by the owner's `PUT /api/chat/yolo-ack`), and the picker skips the
confirmation when either that record or the browser's own copy says so; the
browser copy stays out of the UI-preferences backup, and a Shared Crew teammate
keeps the per-browser copy only.

## Why

- The acknowledgement lived only in per-origin browser storage, so after an
  update or on any fresh browser profile the confirmation came back, while every
  other setting was restored from the host backup.
- That backup lives in the agent-writable data home, so restoring the
  acknowledgement from it would let an agent pre-satisfy a human safety gate;
  the exclusion stays.
- A record on the keystone floor, written only by an owner-gated dashboard
  route, keeps the acknowledgement where the agent cannot write it, the
  placement the other owner authorization records use.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38005 -- the report and the
  options considered.
- https://github.com/kirodotdev/KiroCrew/issues/38005#issuecomment-7074709077 --
  the maintainer's decision.
