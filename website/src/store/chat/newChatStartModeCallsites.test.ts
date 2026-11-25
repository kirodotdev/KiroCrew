/**
 * Call-site ratchet for the person's stored approval start mode
 * (`dashboard.default_approval_mode`, sent as `start_mode_default`).
 *
 * The server seeds it only on a create that asks, so every chat create in the
 * dashboard is one of two kinds:
 *
 * - START MODE -- the person's own plain New chat. It asks: `createSlot` with
 *   `startModeDefault: true` or `crewModeDefault: true`, or `api.createChatSlot`
 *   with the flag as its last argument.
 * - OTHER -- an app's page, a hand-off, a recreate, a forwarder of a caller's
 *   arguments, or the create helper itself. It keeps how it starts today.
 *
 * The table pins both counts per file, so a new create site fails here until
 * someone decides which kind it is.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

type Counts = { startMode: number; other: number; reason?: string }

const PINNED: Record<string, Counts> = {
  'src/apps/papyrus/PapyrusPage.tsx': { startMode: 0, other: 1, reason: 'app' },
  'src/apps/personal-shopper/PersonalShopperPage.tsx': { startMode: 0, other: 1, reason: 'app' },
  'src/apps/pptx-maker/PptxMakerPage.tsx': { startMode: 0, other: 1, reason: 'app' },
  'src/app-sdk/hostSessions.ts': { startMode: 0, other: 1, reason: 'app SDK' },
  'src/components/AgentSwitchNoticeBanner.tsx': { startMode: 0, other: 1, reason: 'a chat on the agent a notice names' },
  'src/components/ArtifactSendToSession.tsx': { startMode: 0, other: 1, reason: 'hand-off of an artifact' },
  'src/components/EmbedTabStrip.tsx': { startMode: 1, other: 0 },
  'src/components/SessionGridView.tsx': { startMode: 1, other: 0 },
  'src/components/commandBar/CommandBarOverlayRootRows.tsx': { startMode: 1, other: 0 },
  'src/components/commandBar/useCommandBarOverlayActivation.ts': { startMode: 0, other: 1, reason: 'an app-contributed row' },
  'src/components/commandPalette/paletteActions.ts': { startMode: 1, other: 0 },
  'src/components/commandPalette/providers/actionsProvider.ts': { startMode: 1, other: 0 },
  'src/components/commandPalette/providers/recentsProvider.ts': { startMode: 1, other: 0 },
  'src/components/milestones/useMilestones.ts': { startMode: 1, other: 0 },
  'src/hooks/useSceneInteraction.tsx': { startMode: 1, other: 0 },
  'src/lib/createdChatView.ts': { startMode: 0, other: 1, reason: 'forwards its caller’s arguments' },
  'src/pages/SessionsPage.tsx': { startMode: 1, other: 0 },
  'src/pages/chat-sidebar/create.ts': { startMode: 1, other: 1, reason: 'createLocalChat forwards its caller’s arguments' },
  'src/pages/chat/memoryModeSwitch.ts': { startMode: 0, other: 1, reason: 'recreate of an existing chat' },
  'src/pages/chat/page/ChatEmptyState.tsx': { startMode: 1, other: 0 },
  'src/pages/chat/page/composerSend.ts': { startMode: 1, other: 0 },
  'src/pages/chat/page/launchIntake.ts': { startMode: 0, other: 1, reason: 'error hand-off' },
  'src/pages/chat/page/paneLayout.tsx': { startMode: 2, other: 0 },
  'src/pages/chat/startFromInitialPrompt.ts': { startMode: 0, other: 2, reason: 'hand-off from another session' },
  'src/pages/chat/useChatPageSessionController.ts': { startMode: 1, other: 1, reason: 'an app draft' },
  'src/pages/overview/AgentTemplatesTab.tsx': { startMode: 1, other: 0 },
  'src/pages/useArtifactDetailPageCompanion.ts': { startMode: 0, other: 1, reason: 'artifact companion chat' },
  'src/pages/useChatPageSlotSwitches.ts': { startMode: 0, other: 2, reason: 'Slack-linked thread, worktree scope' },
  'src/shell/shortcuts/shellKeyboard.ts': { startMode: 0, other: 1, reason: 'forwards newChatShortcutArgs, which ask through crewModeDefault' },
  'src/shell/topbar/requestFeature.ts': { startMode: 0, other: 1, reason: 'the Request a feature chat' },
  'src/store/chat/lifecycle.ts': { startMode: 0, other: 1, reason: 'the create helper' },
}

const SRC = join(__dirname, '..', '..')
const CALLS = ['dispatch(createSlot(', 'api.createChatSlot(', 'createChatForView(']

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) yield p
  }
}

function classify(source: string): Counts {
  const counts = { startMode: 0, other: 0 }
  const lines = source.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trimStart()
    if (trimmed.startsWith('*') || trimmed.startsWith('//')) continue
    for (const call of CALLS) {
      let idx = lines[i].indexOf(call)
      while (idx !== -1) {
        // `export async function createChatForView(` is the definition, not a call.
        if (call !== 'createChatForView(' || !lines[i].includes('function createChatForView(')) {
          const window = [lines[i].slice(idx), ...lines.slice(i + 1, i + 8)].join('\n')
          const end = window.indexOf('))')
          const args = end === -1 ? window : window.slice(0, end + 2)
          if (/startModeDefault: true|crewModeDefault: true|, true\)/.test(args)) counts.startMode++
          else counts.other++
        }
        idx = lines[i].indexOf(call, idx + call.length)
      }
    }
  }
  return counts
}

describe('new chat start-mode call-site ratchet', () => {
  it('every chat create is classified: the person’s plain New chat, or another kind', () => {
    const seen: Record<string, Counts> = {}
    for (const file of walk(SRC)) {
      const source = readFileSync(file, 'utf-8')
      if (!CALLS.some(c => source.includes(c))) continue
      const counts = classify(source)
      if (counts.startMode + counts.other === 0) continue
      seen['src/' + file.slice(SRC.length + 1).replaceAll('\\', '/')] = counts
    }
    const expected = Object.fromEntries(
      Object.entries(PINNED).map(([f, { reason: _reason, ...c }]) => [f, c]),
    )
    expect(Object.fromEntries(Object.entries(seen).sort(([a], [b]) => a.localeCompare(b))))
      .toEqual(Object.fromEntries(Object.entries(expected).sort(([a], [b]) => a.localeCompare(b))))
  })
})
