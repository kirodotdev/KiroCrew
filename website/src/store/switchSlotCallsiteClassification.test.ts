/**
 * Call-site classification ratchet for `dispatch(switchSlot(...))` (#6372).
 *
 * Every non-test dispatch site belongs to exactly one class:
 *
 * - ANNOUNCED (`announceOnMissing: true`) — a user-facing gesture on a listed
 *   session reference. A 404 there surfaces the pane ErrorNotice and the
 *   guarded eviction instead of the silent bounce #6372 reports.
 * - KEEP-TARGET (`keepTargetOnMissing: true`) — a caller that just CREATED the
 *   target; its 404 is a create/fetch race on a slot that exists.
 * - PLAIN — a programmatic or self-handling path (deep-link restore, panel
 *   bind, just-created slots, awaited flows with their own error handling).
 *   Silence there is deliberate: these are not gestures on a LISTED session,
 *   or they recover on their own.
 *
 * The table below pins the per-file counts. Adding a `dispatch(switchSlot(`
 * site fails this test until the new site is classified here — that is the
 * point: the classification must be a decision, never a default. When the new
 * site is a user gesture on a session reference, pass `announceOnMissing`;
 * when it self-handles, bump the plain count AND extend the file's reason.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

type Counts = { announced: number; keepTarget: number; plain: number; reason: string }

/** file -> pinned counts. `reason` documents why the PLAIN sites stay silent. */
const PINNED: Record<string, Counts> = {
  'src/App.tsx': { announced: 2, keepTarget: 0, plain: 0, reason: 'popout nav-intent suppliers announce' },
  'src/app-sdk/ChatPanel.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot the host supplied programmatically' },
  'src/apps/auto-improvement/lib/agentSession.ts': { announced: 0, keepTarget: 0, plain: 2, reason: 'awaited unwrap() flows with their own catch/recovery' },
  'src/apps/command-bar/CommandBarOverlay.tsx': { announced: 1, keepTarget: 1, plain: 0, reason: 'picker announces; create path keeps target on the create/fetch race' },
  'src/apps/issue-radar/lib/agentSession.ts': { announced: 0, keepTarget: 0, plain: 2, reason: 'awaited unwrap() flows with their own catch/recovery' },
  'src/apps/issue-radar/views/CrewPageView.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'awaited unwrap() with page-level error handling' },
  'src/apps/papyrus/CoAuthorPanel.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot supplied programmatically' },
  'src/components/ArtifactChatPanel.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot supplied programmatically' },
  'src/components/ChatInput.tsx': { announced: 1, keepTarget: 0, plain: 0, reason: 'mic-owner status-row click announces' },
  'src/components/commandPalette/providers/recentsProvider.ts': { announced: 1, keepTarget: 0, plain: 0, reason: 'palette recents row announces' },
  'src/components/notifications/NotificationDetailPanel.tsx': { announced: 3, keepTarget: 0, plain: 2, reason: 'go-to-chat buttons announce; the two plain sites switch to a slot a server API call resolved moments before, inside try/catch' },
  'src/hooks/useKeyboardShortcuts.ts': { announced: 1, keepTarget: 0, plain: 0, reason: 'keyboard session jump announces' },
  'src/hooks/useSceneInteraction.tsx': { announced: 1, keepTarget: 0, plain: 0, reason: 'worlds-scene click announces' },
  'src/hooks/useSessionActions.ts': { announced: 0, keepTarget: 0, plain: 1, reason: 'switches to a just-created slot (data.key)' },
  'src/pages/ArtifactDetailPage.tsx': { announced: 0, keepTarget: 0, plain: 2, reason: 'in-page nav intent + just-created slot; both self-handle within the page' },
  'src/pages/ChatPage.tsx': { announced: 2, keepTarget: 1, plain: 7, reason: 'flyout row + split-collapse announce; create path keeps target; the plain sites are deep-link restore, just-created slots, side-chat wiring and awaited flows with in-page error UI' },
  'src/pages/ChatSidebar.tsx': { announced: 3, keepTarget: 0, plain: 0, reason: 'sidebar rows announce' },
  'src/pages/ProjectsPage.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'switches to a just-created slot' },
  'src/pages/chat/SubagentRunCard.tsx': { announced: 1, keepTarget: 0, plain: 0, reason: 'run-card session link announces' },
  'src/pages/chat/WorkflowRunCard.tsx': { announced: 1, keepTarget: 0, plain: 0, reason: 'run-card session link announces' },
  'src/pages/chat/useChatPageSessionController.ts': { announced: 2, keepTarget: 0, plain: 5, reason: 'tab-strip select + foreground open-in-tab announce; the plain sites are close-tab successor selection, URL deep-link restore (x2), mount re-sync and tab fallback — programmatic, not gestures on a listed reference' },
  'src/pages/overview/PromptsTab.tsx': { announced: 0, keepTarget: 0, plain: 1, reason: 'switches to a just-created slot' },
  'src/store/chatSlice.ts': { announced: 0, keepTarget: 0, plain: 1, reason: 'deleteSlot internal fallback navigation; self-handles via unwrap().catch' },
}

const SRC = join(__dirname, '..')

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) yield p
  }
}

function classify(source: string): { announced: number; keepTarget: number; plain: number } {
  const counts = { announced: 0, keepTarget: 0, plain: 0 }
  const lines = source.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trimStart()
    // Skip comment lines: a doc comment naming the pattern is not a call site.
    if (trimmed.startsWith('*') || trimmed.startsWith('//')) continue
    let idx = 0
    for (;;) {
      idx = lines[i].indexOf('dispatch(switchSlot(', idx)
      if (idx === -1) break
      // The argument can span lines; look at a bounded window after the call.
      const window = [lines[i].slice(idx), lines[i + 1] ?? '', lines[i + 2] ?? ''].join('\n').slice(0, 260)
      if (window.includes('announceOnMissing: true')) counts.announced++
      else if (window.includes('keepTargetOnMissing: true')) counts.keepTarget++
      else counts.plain++
      idx += 'dispatch(switchSlot('.length
    }
  }
  return counts
}

describe('switchSlot call-site classification ratchet (#6372)', () => {
  it('every dispatch site is classified: announced, keep-target, or documented-plain', () => {
    const seen: Record<string, { announced: number; keepTarget: number; plain: number }> = {}
    for (const file of walk(SRC)) {
      const rel = file.slice(SRC.length + 1).replaceAll('\\', '/')
      const source = readFileSync(file, 'utf-8')
      if (!source.includes('dispatch(switchSlot(')) continue
      const counts = classify(source)
      // A file whose only mention is inside a comment has zero call sites and
      // needs no classification row (NavigationLeaveGuard's doc comment).
      if (counts.announced + counts.keepTarget + counts.plain === 0) continue
      seen['src/' + rel] = counts
    }
    const actual = Object.fromEntries(
      Object.entries(seen).map(([f, c]) => [f, c]).sort(([a], [b]) => a.localeCompare(b)),
    )
    const expected = Object.fromEntries(
      Object.entries(PINNED).map(([f, { reason: _reason, ...c }]) => [f, c]).sort(([a], [b]) => a.localeCompare(b)),
    )
    // One deep equal: a new file, a moved site, or a class change all surface
    // here with the file name in the diff.
    expect(actual).toEqual(expected)
  })
})
