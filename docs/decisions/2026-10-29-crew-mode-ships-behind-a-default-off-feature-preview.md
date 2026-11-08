# Crew Mode ships behind a default-off Feature Preview

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-29
Supersedes: 2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md (in part: the preview-off state, and the visible name of the switch)
Supersedes: 2026-10-28-the-crew-gradient-marks-crew-mode-surfaces.md

## Decision

The dashboard's Crew Mode is a Feature Preview in Settings > Developer > Feature
Previews, off by default. Off, the dashboard shows no Crew Mode surface and no crew
gradient: no composer switch or welcome, no CREW MODE row or eyebrow, no Crew Mode
filter, no Crew tab or crew member breadcrumb. Crew members list and alert as
ordinary sessions, conductor templates are listed in the agent picker, and a chat
already in Crew Mode reads as an ordinary chat on its conductor agent that offers no
agent switch once it has a message.

On, the switch and its surfaces are named **Ultra Mode** in user-facing text (code,
config keys and routes keep `crew_mode`); the Crew tab and crew members keep their
names. Crew Mode surfaces wear one solid theme accent instead of the crew gradient,
so the flat-fill rule admits no exception for them. Everything else in the
2026-10-27 entry applies unchanged.

## Why

- Crew Mode is complete, and the owner wants to launch it properly: people opt in
  from the one page that lists unreleased features, and everyone else keeps the
  dashboard they know.
- The owner picked the redesigned Crew page ("V2") over two other prototypes,
  with the Ultra Mode name and a flat accent, for the launch.
- Nothing a person has is hidden or unreachable with the preview off; the gateway's
  Crew Mode rules are unchanged, so turning the preview on shows the same chats.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/25872#issuecomment-6505573759 -- the
  maintainer's ruling, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/25856 -- the issue, a follow-up to
  https://github.com/kirodotdev/KiroCrew/issues/24860.
