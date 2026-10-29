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
 * GESTURE (`gesture: true`) is pinned alongside, and is a separate decision:
 * the user opened this session themselves, by a click, a key or a palette
 * pick in this window. Only those landings record `ChatState.userOpen`, which
 * marks the chat's cron notes read (#23564). Every gesture site also
 * announces, but not every announced site is a gesture: the late `?sid=`
 * recovery in useChatPageSessionController and the two forwarded deep-link
 * and nav-intent suppliers in appShell are the page's own switches.
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

type Counts = { gesture: number; announced: number; keepTarget: number; plain: number; reason: string }

/** file -> pinned counts. `reason` documents why the PLAIN sites stay silent. */
const PINNED: Record<string, Counts> = {
  'src/shell/appShell.tsx': { gesture: 0, announced: 2, keepTarget: 0, plain: 0, reason: 'popout nav-intent suppliers announce; a forwarded intent or native deep link is not a gesture in this window, so neither marks one' },
  'src/app-sdk/ChatPanel.tsx': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot the host supplied programmatically' },
  'src/apps/auto-improvement/lib/agentSession.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 2, reason: 'awaited unwrap() flows with their own catch/recovery' },
  'src/apps/command-bar/CommandBarOverlayRootRows.tsx': { gesture: 2, announced: 2, keepTarget: 0, plain: 0, reason: 'attention row and recent row announce' },
  'src/apps/command-bar/useCommandBarOverlayActivation.ts': { gesture: 0, announced: 0, keepTarget: 1, plain: 0, reason: 'create path keeps target on the create/fetch race' },
  'src/apps/issue-radar/lib/agentSession.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 2, reason: 'awaited unwrap() flows with their own catch/recovery' },
  'src/apps/issue-radar/views/CrewPageView.tsx': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'awaited unwrap() with page-level error handling' },
  'src/apps/papyrus/CoAuthorPanel.tsx': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot supplied programmatically' },
  'src/components/ArtifactChatPanel.tsx': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'panel binds to a slot supplied programmatically' },
  'src/components/ChatInput.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'mic-owner status-row click announces' },
  'src/components/ImportSessionItem.tsx': { gesture: 1, announced: 1, keepTarget: 1, plain: 0, reason: 'on upload the just-imported slot is new, so a 404 is a create/fetch race (#6309); the shell notice\'s later Open is a click, so it announces and is a gesture' },
  'src/components/commandPalette/providers/recentsProvider.ts': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'palette recents row announces' },
  'src/components/notifications/NotificationDetailPanel.tsx': { gesture: 3, announced: 3, keepTarget: 0, plain: 0, reason: 'go-to-chat buttons announce; the two to-chat hand-offs open through openCreatedSlot' },
  'src/hooks/useKeyboardShortcuts.ts': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'keyboard session jump announces' },
  'src/hooks/useSceneInteraction.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'worlds-scene click announces' },
  'src/hooks/useSessionActions.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'switches to a just-created slot (data.key)' },
  'src/pages/ChatPage.tsx': { gesture: 2, announced: 2, keepTarget: 1, plain: 1, reason: 'flyout row + split-collapse announce; the split-collapse site is a gesture only for a pane\'s open-full click, not the grid collapsing itself; create path keeps target; the plain site is the comment-batch send moving to the tab it targets, which reports its own verdict' },
  'src/pages/ChatSidebar.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'the broadcast panel\'s "Not confirmed · open" opens a LISTED row the way that row\'s own click does, so it announces' },
  'src/pages/chat-sidebar/CrewGroups.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'the crew header\'s attention button opens a LISTED row the way that row\'s own click does, so it announces' },
  'src/pages/chat-sidebar/SessionRow.tsx': { gesture: 3, announced: 3, keepTarget: 0, plain: 0, reason: 'every site is a gesture on a LISTED session row, so all three announce: the row\'s keyboard activation, its click, and the source chip\'s onActivateSlot' },
  'src/pages/chat-sidebar/folderBlocks.tsx': { gesture: 2, announced: 2, keepTarget: 0, plain: 0, reason: 'every site is a gesture on a LISTED folder, so both announce: the two ways to reach a folder\'s conductor -- the item in both folder menus and the chat button in the folder row\'s own hover cluster' },
  'src/pages/chat/SubagentRunCard.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'run-card session link announces' },
  'src/pages/chat/WorkflowRunCard.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'run-card session link announces' },
  'src/pages/chat/command-center/CrewWorkerList.tsx': { gesture: 1, announced: 1, keepTarget: 0, plain: 0, reason: 'opening a Crew Mode worker, or going back to its root, from the side panel is a click on a listed session: it announces and is a gesture' },
  'src/pages/chat/memoryModeSwitch.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'switches to the just-created memory-mode replacement, awaited with in-page error UI' },
  'src/pages/chat/useChatPageSessionController.ts': { gesture: 3, announced: 4, keepTarget: 0, plain: 7, reason: 'tab-strip select + foreground open-in-tab + late-frame deep-link recovery + a short-name notice row announce, and all but the late-frame recovery are gestures; the plain sites are close-tab successor selection, URL deep-link restore (x2), the short-name deep link opening a slot it just found in the live roster, mount re-sync, tab fallback and app-launch activation with its own sidError notice' },
  'src/pages/overview/PromptsTab.tsx': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'switches to a just-created slot' },
  'src/pages/useArtifactDetailPageCompanion.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 2, reason: 'in-page nav intent + just-created slot; both self-handle within the page' },
  'src/pages/useChatPageSlotSwitches.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 5, reason: 'the page\'s own slot-switching flows, each awaited with in-page error UI or on a slot it just created or found: fork, the signed chat link (reconnect + just-linked slot), the follow-up session and the split pane\'s side chat' },
  'src/store/chat/openCreatedSlot.ts': { gesture: 0, announced: 0, keepTarget: 1, plain: 0, reason: 'opens a slot a server route just created, so a 404 is a create/fetch race' },
  'src/store/chatSlice.ts': { gesture: 0, announced: 0, keepTarget: 0, plain: 1, reason: 'deleteSlot internal fallback navigation; self-handles via unwrap().catch' },
}

const SRC = join(__dirname, '..')

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) yield p
  }
}

function classify(source: string): { gesture: number; announced: number; keepTarget: number; plain: number } {
  const counts = { gesture: 0, announced: 0, keepTarget: 0, plain: 0 }
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
      // Orthogonal to the three classes below: whether the site is the user
      // opening the session themselves (see GESTURE in the header).
      if (window.includes('gesture: true')) counts.gesture++
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
    const seen: Record<string, { gesture: number; announced: number; keepTarget: number; plain: number }> = {}
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

  // The ratchet above counts `dispatch(switchSlot(` sites, so a hand-off that
  // opens the session a `/to-chat` route created by navigating alone
  // (`navigate('/chat?slot=' + key)`) has no site to count and slips past it.
  // Such a caller never stamps the key as a membership add, and a full slots
  // frame serialized before the create drops it after the switch. Pin every
  // file that calls a `/to-chat` route to the number of `openCreatedSlot(`
  // calls it makes: a new caller, or one that stops opening through the
  // helper, fails here.
  it('every /to-chat caller opens the session through openCreatedSlot', () => {
    const TO_CHAT_CALL = /\bapi\.(cronToChat|taskRunToChat)\(/g
    const seen: Record<string, { toChat: number; openCreated: number }> = {}
    for (const file of walk(SRC)) {
      const rel = 'src/' + file.slice(SRC.length + 1).replaceAll('\\', '/')
      const code = readFileSync(file, 'utf-8')
        .split('\n')
        .filter(l => !/^\s*(\*|\/\/)/.test(l))
        .join('\n')
      const toChat = code.match(TO_CHAT_CALL)?.length ?? 0
      if (toChat === 0) continue
      seen[rel] = { toChat, openCreated: code.match(/\bopenCreatedSlot\(dispatch,/g)?.length ?? 0 }
    }
    expect(seen).toEqual({
      'src/components/notifications/NotificationDetailPanel.tsx': { toChat: 2, openCreated: 2 },
      'src/hooks/useCronActions.ts': { toChat: 1, openCreated: 1 },
      'src/pages/ProjectsPage.tsx': { toChat: 1, openCreated: 1 },
    })
  })
})
