# UX evidence — opt-in "Open session" deep-link button (PR #16114)

`open-session-button-slack.png` renders the EXACT Block Kit payload that
`src/kiro_crew/dashboard/handlers/messaging.py::_session_link_blocks` emits
(captured verbatim from the function), on a Slack-faithful message surface:

- **Form A** — the "Open session" button riding the primary owner-DM message
  (one message, one notification): a text section + the actions block + the
  sign-in-window context line.
- **Form B** — the trailing follow-up: the same button posted as its own
  message, the fallback when the primary cannot carry Block Kit blocks (a caller
  using `options`) or Slack rejected the combined post.

The only user-visible surface this PR adds lives in Slack, not the website, so
there is no `website/**` pixel delta to capture; this committed render is the
evidence the UX gate reads for the Slack control.
