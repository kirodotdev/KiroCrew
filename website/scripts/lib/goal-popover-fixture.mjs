/**
 * The one chat slot the automation-popover harnesses photograph, and its
 * transcript. Shared so the two goal-editor harnesses
 * (`capture-automation-popover-default.mjs`, `capture-goal-stop-file-help-10458.mjs`)
 * describe the same idle session instead of each carrying a copy of it -- the
 * frontend lint's duplicate-code gate (`jscpd`) is a hard zero, and a fixture
 * block is exactly the shape that trips it.
 *
 * The slot is idle and two messages deep: enough transcript for the composer to
 * render as it does in real use, and nothing running, so the popover's own
 * state (what is armed, which view opens) is the only variable in the frame.
 */

export const GOAL_POPOVER_PROJECT = '/home/user/workspace/notes'

/** `GET /api/chat/slots` rows: one idle slot under `key`. */
export function goalPopoverSlots(key, project = GOAL_POPOVER_PROJECT) {
  return [{
    key,
    title: 'Trim the flaky-test backlog',
    running: false,
    last_message: 'Two candidates left.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project,
    folder_id: '',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  }]
}

/** `GET /api/chat/slots/<key>` detail for that slot. */
export function goalPopoverDetail(project = GOAL_POPOVER_PROJECT) {
  return {
    running: false,
    has_more: false,
    total: 2,
    queue: [],
    project,
    messages: [
      { role: 'user', ts: Date.now() / 1000 - 600, content: 'Which flaky test should we take first?' },
      { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Two candidates left.' },
    ],
  }
}
