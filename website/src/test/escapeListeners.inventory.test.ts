/**
 * Every file with a page-wide (document or window) key listener that handles
 * Escape either answers it through the Escape stack (hooks/useEscapeLayer.ts,
 * issue #25202) or is listed here with a reason.
 *
 * Page-wide Escape listeners that each pick their own phase and target collide:
 * which one gets a press depends on capture vs bubble and registration order,
 * and one press can close two surfaces. The stack gives a press one owner. A new
 * page-wide Escape listener fails here until it registers a layer or is listed.
 *
 * shortcut: a file counts when it has a document/window keydown or keyup
 * listener and mentions Escape anywhere, so a listener added through another
 * target variable is missed, and a file whose Escape lives only in JSX is
 * counted; upgrade to a per-listener scan if that proves too loose.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..')

const GLOBAL_KEY_LISTENER = /\b(?:document|window)\.addEventListener\(\s*['"]key(?:down|up)['"]/
const ON_STACK = /\buseEscapeLayer\b/

/** Page-wide Escape listeners that stay off the stack on purpose. */
const OFF_STACK: Record<string, string> = {
  'hooks/useDialogFocusTrap.ts':
    'dialogs answer Escape on window capture, ahead of the stack, on purpose (#25202 decision: dialogs stay)',
  'components/Modal.tsx':
    'window bubble and skips a defaultPrevented Escape, so a layer that takes the press keeps the modal open',
  'hooks/useKeyboardShortcuts.ts':
    'the speech read-back stop must hear every Escape and never consumes it; it marks the press instead',
  'components/notifications/NotificationBanner.tsx':
    'acts only on an unclaimed press: window bubble, skips a defaultPrevented Escape',
}

/** Listeners that predate the stack. They move onto it when they collide
 *  (#25202); this list may only shrink. */
const PREDATES_STACK = [
  'api/qrScan.ts',
  'apps/crew-companion/BreathingOverlay.tsx',
  'apps/crew-companion/ContextMenu.tsx',
  'apps/crew-companion/GalleryPanel.tsx',
  'apps/crew-companion/panel.tsx',
  'apps/design-critique/DesignCritiquePage.tsx',
  'apps/design-tweak/useDesignTweakPageState.ts',
  'apps/dev-fleet/DevFleetPageMenus.tsx',
  'apps/file-explorer/FileExplorerPage.tsx',
  'apps/issue-radar/components/PrList.tsx',
  'apps/md-notebook/ConfirmDialog.tsx',
  'apps/md-notebook/SettingsPage.tsx',
  'apps/mochi/src/renderer/ContextMenu.tsx',
  'apps/mochi/src/renderer/PackEditor.tsx',
  'apps/mochi/src/renderer/WatchlistPanel.tsx',
  'apps/spec-builder/components/SpecDetail.tsx',
  'components/AgentImportFlow.tsx',
  'components/AgentSelector.tsx',
  'components/AgentSkillsEditor.tsx',
  'components/ArtifactPanel.tsx',
  'components/ChatDropOverlay.tsx',
  'components/CommandPalette.tsx',
  'components/CommentThreadPopover.tsx',
  'components/DiagramLightbox.tsx',
  'components/FrameLinkChip.tsx',
  'components/ImageCommentLayer.tsx',
  'components/InfoTip.tsx',
  'components/InlineStyledConfirm.tsx',
  'components/InstantTip.tsx',
  'components/MarkdownPanel.tsx',
  'components/MemoryModeChip.tsx',
  'components/MicSourceMenu.tsx',
  'components/OnboardingFlow.tsx',
  'components/PasteHoverLayer.tsx',
  'components/PastedChip.tsx',
  'components/RedactionCards.tsx',
  'components/SelectionToolbar.tsx',
  'components/SessionControlHost.tsx',
  'components/ShortcutsModal.tsx',
  'components/SnipOverlay.tsx',
  'components/UpdateFoundModal.tsx',
  'components/UpdateModal.tsx',
  'components/canvas/ShapeToolbar.tsx',
  'components/canvas/SwatchPicker.tsx',
  'components/canvas/TextSizeMenu.tsx',
  'components/chat-input/composerActions.tsx',
  'components/chat-input/keyboard.ts',
  'components/chat-input/typingLockKeys.ts',
  'components/chat-input/voice.ts',
  'components/markdown/Lightbox.tsx',
  'components/milestones/MilestonesSheet.tsx',
  'hooks/useCommandPalette.ts',
  'hooks/useGlobalHotkey.ts',
  'hooks/useMenuKeyboard.ts',
  'hooks/useMessageSearch.ts',
  'hooks/usePanelToggleShortcuts.ts',
  'hooks/useQuickSearchShortcut.ts',
  'hooks/useSceneInteraction.tsx',
  'lib/quietFocusReturn.ts',
  'linkPreview/LinkPreviewLayer.tsx',
  'messageActions/MessageActionLayer.tsx',
  'pages/ChannelPage.tsx',
  'pages/ChatSidebar.tsx',
  'pages/chat-sidebar/NeedsMeHoverCard.tsx',
  'pages/chat-sidebar/rename.ts',
  'pages/chat/McpInfoButton.tsx',
  'pages/chat/page/welcomeState.ts',
  'pages/connections/ConnectionsPageCard.tsx',
  'pages/knowledge/index.tsx',
  'pages/members/BotSessionsPanelTab.tsx',
  'pages/members/CrewProfilePanel.tsx',
  'pages/settings/AboutPanel.tsx',
  'pages/useArtifactDetailPageEditor.ts',
  'shell/appShell.tsx',
  'shell/notifications/notificationSheet.tsx',
  'shell/topbar/metricsReadout.tsx',
]

function escapeListeners(): { file: string; onStack: boolean }[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p) && !/\.(test|stories)\./.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__'))
    .map(file => ({ file, src: readFileSync(join(SRC, file), 'utf8') }))
    .filter(({ src }) => GLOBAL_KEY_LISTENER.test(src) && src.includes('Escape'))
    .map(({ file, src }) => ({ file, onStack: ON_STACK.test(src) }))
}

describe('page-wide Escape listeners go through the Escape stack', () => {
  const found = escapeListeners()
  const listed = new Set([...Object.keys(OFF_STACK), ...PREDATES_STACK])

  it('finds the listeners it is meant to check', () => {
    // useListKeyboardNav keeps a document listener for its arrow keys and
    // answers Escape through the stack; the other two stay off it.
    const live = new Map(found.map(f => [f.file, f.onStack]))
    expect(live.get('hooks/useListKeyboardNav.ts')).toBe(true)
    expect(live.get('hooks/useDialogFocusTrap.ts')).toBe(false)
    expect(live.get('components/Modal.tsx')).toBe(false)
  })

  it('puts every unlisted page-wide Escape listener on the stack', () => {
    const off = found.filter(f => !f.onStack && !listed.has(f.file)).map(f => f.file)
    expect(off, 'answer Escape through useEscapeLayer, or list the file in OFF_STACK with a reason').toEqual([])
  })

  it('lists only files that are still off the stack', () => {
    const live = new Map(found.map(f => [f.file, f.onStack]))
    for (const file of listed) {
      expect(live.has(file), `${file} no longer has a page-wide Escape listener; drop it`).toBe(true)
      expect(live.get(file), `${file} is on the stack now; drop it`).toBe(false)
    }
  })
})
