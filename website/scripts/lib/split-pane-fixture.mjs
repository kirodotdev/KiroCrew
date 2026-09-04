/**
 * Shared fixture wiring for capture harnesses that photograph the session
 * grid: stubs the routes a two-pane split view needs (`session_grid` config,
 * one transcript per pane, empty sessions/pins) and seeds the persisted
 * split layout a user's ⌘D would have written.
 *
 * Extracted from capture-chatpane-followup-options.mjs when
 * capture-chatpane-plan-dispatch.mjs needed the identical stanza (the jscpd
 * gate runs at 0% duplication over scripts/).
 */
import { stubDashboardApi, json } from './stub-dashboard-api.mjs'

/**
 * @param page Playwright page
 * @param opts.slots        dashboard slot fixtures (array)
 * @param opts.transcripts  map of slot key -> messages array
 * @param opts.layout       mc-split-layouts object to persist
 * @param opts.extra        optional (path, route) handler tried FIRST,
 *                          for harness-specific routes (e.g. POST intercepts)
 */
export async function stubSplitPanes(page, { slots, transcripts, layout, extra = null }) {
  await stubDashboardApi(page, {
    folders: [], slots,
    extra: async (path, route) => {
      if (extra && (await extra(path, route))) return true
      if (path === '/api/dashboard/config') {
        // session_grid gates split view; the rest mirrors the shared default.
        await json(route, {
          restore_sessions: false, restore_window_minutes: 30,
          merge_queued_messages: false, widget_density: 'more', session_grid: true,
        })
        return true
      }
      for (const [slot, messages] of Object.entries(transcripts)) {
        if (path === `/api/chat/slots/${slot}`) {
          await json(route, { messages, has_more: false, total: messages.length })
          return true
        }
      }
      if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
      if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
      return false
    },
  })
  // Added after stubDashboardApi so it runs after that script's localStorage.clear().
  await page.addInitScript(l => {
    localStorage.setItem('mc-split-layouts', JSON.stringify(l))
  }, layout)
}
