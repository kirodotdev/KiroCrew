# Screenshots for #25891 / PR #25932 — Crew Mode dispatch notice

Real renders of the unmodified components, produced from the repo's own vitest
fixtures via the isolated loopback capture harness
(`website/capture/crew-dispatch-notice.{tsx,html}` +
`website/capture/shoot-crew-dispatch-notice.mjs`, 127.0.0.1-only). No gateway,
kiro-cli or token is involved.

- `01-notice-row-open-crew-tab-enabled.png` — the dispatch-notice row in a
  conductor transcript, "Open Crew tab" button enabled.
- `02-notice-row-button-disabled-embedded-chat.png` — the same row in a
  side/embedded chat, button disabled (no host reveal wired).
- `03-crew-tab-open-after-click.png` — the Crew (Workers) tab open on its List
  view, the surface the button reveals (dispatched members as rows).
- `03b-crew-tab-status-view.png` — the same Crew tab's Status landing view.

This branch is screenshot hosting only; it is never opened as a PR.
