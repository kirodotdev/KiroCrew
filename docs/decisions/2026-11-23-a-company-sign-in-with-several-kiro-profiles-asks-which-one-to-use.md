# A company sign-in with several Kiro profiles asks which one to use

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-23

## Decision

When a company (IAM Identity Center) sign-in can use more than one Kiro
profile, the dashboard sign-in stops after the approval and asks which profile
to use, as a step of the sign-in that lists the profiles of every Kiro region
with the one already signed in with (else the first) preselected; with one
profile nothing changes, and `kirocrew kas login` never prompts: it keeps the
profile already signed in with when it is listed, else the first, and says
which.

## Why

The issue asks to choose among profiles that carry their own credit pools. One
account can hold profiles on two control planes, so a picker that hides a
region fails it. A picker in the Kiro Account modal after sign-in was not
chosen: it needs a list route, a write route and a second way to change the
stored profile outside the sign-in's own save (its sign-in marker, races and
audit line), while the sign-in step reuses the existing poll.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36638#issuecomment-7088648503 (the maintainer decision)
- https://github.com/kirodotdev/KiroCrew/issues/36638
- https://github.com/kirodotdev/KiroCrew/pull/29167 (the first version of this selector)
