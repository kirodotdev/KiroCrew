import { safeSetItem } from '../utils/safeStorage'
import { Fragment, useEffect, useState } from 'react'
import { X, Keyboard } from 'lucide-react'
import { DEFAULT_SHORTCUTS, formatShortcut, formatChordCaps, resolveShortcutDef, SHORTCUT_GROUPS, shortcutGroupLabel, shortcutLabel, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, IS_MAC, MAC_CTRL_DIGITS_KEY, type ShortcutDef } from '../hooks/useKeyboardShortcuts'
import { useQuickSearchShortcut } from '../hooks/useQuickSearchShortcut'
import { usePanelToggleShortcuts } from '../hooks/usePanelToggleShortcuts'
import { useGlobalHotkey } from '../hooks/useGlobalHotkey'
import { formatQuickSearchKeys, formatChordKeys } from '../lib/quickSearchShortcut'
import { PANEL_TOGGLE_IDS, type PanelToggleId } from '../lib/panelToggleShortcuts'
import { formatAcceleratorKeys } from '../lib/globalHotkey'
import { isElectron } from '../lib/electron'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import { Toggle } from './ui'

import { i18nT } from '../i18n/t'
/**
 * Shortcut group ids + heading resolver, re-exported from the hook that owns
 * them.
 *
 * `useKeyboardShortcuts` is the single source of truth: `SHORTCUT_GROUPS` is the
 * canonical id set and display order, and the ids are the discriminant matched
 * against `ShortcutDef.group` in `groupShortcuts()` below — never display copy.
 * The heading is resolved by `shortcutGroupLabel()` at render. They are re-exported
 * here because `Settings → Shortcuts` (`pages/settings/ShortcutsPanel.tsx`) imports
 * the group list from this module.
 */
export { SHORTCUT_GROUPS, shortcutGroupLabel }

export function Kbd({ children }: { children: string }) {
  return <kbd className="inline-flex items-center justify-center min-w-[24px] h-6 px-1.5 rounded-md bg-bg border border-border text-[12px] font-mono font-medium text-text-strong shadow-sm">{children}</kbd>
}

/**
 * Shortcut preference state (enable/disable + the macOS Ctrl-vs-Option digit
 * binding), persisted to localStorage and broadcast via
 * SHORTCUTS_ENABLED_EVENT. Shared by the Alt+K modal and Settings → Shortcuts
 * so both surfaces stay in sync.
 */
export function useShortcutPrefs() {
  const [enabled, setEnabled] = useState(() => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
  const [macCtrl, setMacCtrl] = useState(() => localStorage.getItem(MAC_CTRL_DIGITS_KEY) !== '0')

  const toggle = (v: boolean) => {
    safeSetItem(SHORTCUTS_ENABLED_KEY, v ? '1' : '0')
    setEnabled(v)
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
  }

  const toggleMacCtrl = (v: boolean) => {
    safeSetItem(MAC_CTRL_DIGITS_KEY, v ? '1' : '0')
    setMacCtrl(v)
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
  }

  return { enabled, macCtrl, toggle, toggleMacCtrl }
}

/**
 * Shortcuts in `group`, with the Mac Ctrl/Option digit display adjustment
 * applied and the user's registry overrides resolved (a rebound entry shows the
 * chord the user chose; an entry the user unbound is not listed).
 */
export function groupShortcuts(group: string, macCtrl: boolean): ShortcutDef[] {
  // The Instances chord (⌘/Ctrl+digit) only works in the Electron shell — in a
  // plain browser those chords are reserved for browser tab switching and the
  // handler never binds (see useInstanceShortcuts). Don't advertise a binding
  // the host environment will steal.
  if (group === 'remote-crews' && !isElectron) return []
  const out: ShortcutDef[] = []
  for (const s of DEFAULT_SHORTCUTS.filter(s => s.group === group)) {
    // When Mac user toggles back to Alt+digit, adjust the display
    if (IS_MAC && !macCtrl && s.id.startsWith('chat-') && s.ctrl) {
      out.push({ ...s, ctrl: false, alt: true })
      continue
    }
    // A registry-dispatched entry renders its LIVE binding. `resolveShortcutDef`
    // is null for an unknown id (an extension-seam panel registration, which
    // has no registry record) — those keep their static def — and for an entry
    // the user cleared to unbound, which is then omitted.
    const live = resolveShortcutDef(s.id)
    if (live) out.push(live)
    else if (s.label !== undefined) out.push(s)
  }
  return out
}

/**
 * How one entry's chords are shown. The primary is the advertised chord and the
 * aliases follow it muted — except in a BROWSER host for a browser-reserved
 * chord (⌘N, ⌘W): the browser takes that keystroke before the page sees it, so
 * advertising it first would advertise a chord that does not work here. There
 * the alias leads and the reserved chord is the muted one, with the reason.
 */
export interface SecondaryChord {
  caps: string[]
  /**
   * Inline tag rendered after the caps. Set on a browser-reserved chord shown in
   * a browser host ("Desktop app"): a muted chord otherwise reads as "also
   * works", and this one does not work here — the tag says where it does.
   */
  tag?: string
}

export function displayChords(def: ShortcutDef): { primary: string[]; secondary: SecondaryChord[]; reservedInBrowser: boolean } {
  const primary = formatShortcut(def).split(' + ')
  const aliases = (def.aliases ?? []).map(a => ({ caps: formatChordCaps(a, def.id) }))
  const reservedInBrowser = !!def.browserReserved && !isElectron
  if (reservedInBrowser && aliases.length > 0) {
    // No tooltip: the explainer renders inline directly under the last demoted
    // row (ShortcutGroupRows), so a title here would only restate it.
    const reserved: SecondaryChord = { caps: primary, tag: i18nT('components.shortcutsModal.desktop_app_only') }
    return { primary: aliases[0].caps, secondary: [reserved, ...aliases.slice(1)], reservedInBrowser }
  }
  return { primary, secondary: aliases, reservedInBrowser }
}

/**
 * The rows of one group. In a browser host the explainer for the demoted
 * browser-reserved chords renders DIRECTLY UNDER the last such row, not at the
 * group's end: the Actions group runs ~16 rows past "Close session", and a
 * sentence about "this shortcut" sitting under "Stop speaking" points at nothing.
 * `hintClass` lets the two hosts (modal, Settings card) keep their own spacing.
 */
export function ShortcutGroupRows({ entries, hintClass }: { entries: readonly ShortcutDef[]; hintClass: string }) {
  const lastReserved = isElectron ? -1 : entries.reduce((acc, e, i) => (e.browserReserved ? i : acc), -1)
  return (
    <>
      {entries.map((s, i) => (
        <Fragment key={s.id}>
          <ShortcutDefRow def={s} />
          {i === lastReserved && (
            <div className={hintClass}>{i18nT('components.shortcutsModal.browser_reserved_hint')}</div>
          )}
        </Fragment>
      ))}
    </>
  )
}

/**
 * One reference row: label left, key caps right. `secondary` chords follow the
 * primary after an "or", muted. A legacy alias is just muted (it works too); a
 * browser-reserved chord demoted in a browser host carries an inline tag naming
 * where it works, because muted alone would read as "also works" there.
 */
export function ShortcutRow({ label, keys, secondary }: { label: string; keys: string[]; secondary?: readonly SecondaryChord[] }) {
  return (
    <div className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
      <span className="text-[13px] text-text">{label}</span>
      <span className="flex items-center gap-1 flex-wrap justify-end">
        {keys.map((p, i) => <span key={i} className="flex items-center gap-1">{i > 0 && <span className="text-muted text-[11px]">+</span>}<Kbd>{p}</Kbd></span>)}
        {secondary?.map((sc, j) => (
          <span key={j} className="flex items-center gap-1 opacity-60">
            <span className="text-muted text-[11px] mx-1">{i18nT('components.shortcutsModal.or')}</span>
            {sc.caps.map((p, i) => <span key={i} className="flex items-center gap-1">{i > 0 && <span className="text-muted text-[11px]">+</span>}<Kbd>{p}</Kbd></span>)}
            {/* A div, not a span: "or" and the tag are separate catalog units, and a
                block element ends the inline text run so the i18n render gate never
                sees them glued into one string (it is a flex item, so layout is the same). */}
            {sc.tag && <div className="text-muted text-[10px] uppercase tracking-wider ml-1">{sc.tag}</div>}
          </span>
        ))}
      </span>
    </div>
  )
}

/** A registry entry's row: primary caps, muted aliases, browser-host demotion. */
export function ShortcutDefRow({ def }: { def: ShortcutDef }) {
  const { primary, secondary } = displayChords(def)
  return <ShortcutRow label={shortcutLabel(def)} keys={primary} secondary={secondary} />
}

/**
 * Render a sequence of key caps. `plus` inserts a "+" between caps (a chord like
 * ⌘ + K); without it the caps sit adjacent (the double-⇧ gesture). The cap
 * strings come from {@link formatQuickSearchKeys} / {@link formatChordKeys} —
 * dynamic values, never JSX string literals, so they carry no translatable copy.
 */
export function KeyCapSequence({ caps, plus }: { caps: string[]; plus?: boolean }) {
  return (
    <>
      {caps.map((cap, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && plus && <span className="text-muted text-[11px]">+</span>}
          <Kbd>{cap}</Kbd>
        </span>
      ))}
    </>
  )
}

/**
 * Search Everywhere reference row. Its bindings live outside DEFAULT_SHORTCUTS:
 * the activation gesture is wired in useCommandPalette (not the Alt-based
 * useKeyboardShortcuts handler), so it is documented with this dedicated row.
 *
 * The caps reflect the user's configured preset live (edited in
 * Settings → Shortcuts): the primary gesture, plus the ⌘K / Ctrl+K alias in
 * `double-shift` mode where that alias stays active. A `custom` mode awaiting a
 * recorded chord shows the record prompt.
 */
export function SearchEverywhereRow() {
  const { config } = useQuickSearchShortcut()
  const caps = formatQuickSearchKeys(config)
  // ⌘K / Ctrl+K stays live as an alias in double-shift mode (see
  // useCommandPalette), so advertise it alongside the primary gesture.
  const showAlias = config.mode === 'double-shift'
  return (
    <div className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
      <span className="text-[13px] text-text">{i18nT('components.shortcutsModal.search_everywhere')}</span>
      <span className="flex items-center gap-1">
        {caps.length > 0 ? (
          <KeyCapSequence caps={caps} plus={config.mode !== 'double-shift'} />
        ) : (
          <span className="text-muted text-[11px]">{i18nT('pages.settings.shortcutsPanel.record_prompt')}</span>
        )}
        {showAlias && (
          <>
            <span className="text-muted text-[11px] mx-1">{i18nT('components.shortcutsModal.or')}</span>
            <Kbd>{IS_MAC ? '⌘' : 'Ctrl'}</Kbd>
            <span className="text-muted text-[11px]">+</span>
            <Kbd>{i18nT('components.shortcutsModal.k')}</Kbd>
          </>
        )}
      </span>
    </div>
  )
}

/**
 * The chords that open this reference UNCONDITIONALLY — for the footer's "always
 * works" line and the Settings toggle description. The ⌘/ / Ctrl+/ primary yields
 * to an embedded terminal or an editor that already claimed the key, so naming it
 * here would make the recovery instruction lie exactly where a user is stuck; the
 * Option/Alt aliases fire everywhere. Only when an entry has no Alt alias (a P3
 * rebind cleared it) does the line fall back to whatever primary is bound.
 */
export function shortcutsHelpChords(): string[][] {
  const def = resolveShortcutDef('shortcuts-modal')
  if (!def) return []
  const unconditional = (def.aliases ?? []).filter(a => a.alt && !a.mod && !a.ctrl).map(a => formatChordCaps(a, def.id))
  return unconditional.length > 0 ? unconditional : [formatShortcut(def).split(' + ')]
}

/** {@link shortcutsHelpChords} as one display string: "Ctrl + / or Alt + K". */
export function shortcutsHelpText(): string {
  const sep = IS_MAC ? '' : ' + '
  return shortcutsHelpChords().map(caps => caps.join(sep)).join(` ${i18nT('components.shortcutsModal.or')} `)
}

/**
 * Catalog KEY for each panel-toggle's display label. Kept beside the shortcut
 * display surfaces (not in the pure `panelToggleShortcuts` lib, which carries no
 * i18n) and shared by the Alt+K modal and Settings → Shortcuts so their labels
 * cannot drift.
 */
export const PANEL_TOGGLE_LABEL_KEY: Record<PanelToggleId, string> = {
  'left-sidebar': 'hooks.useKeyboardShortcuts.toggle_left_sidebar',
  'session-panel': 'hooks.useKeyboardShortcuts.toggle_session_panel',
  'side-panel': 'hooks.useKeyboardShortcuts.toggle_side_panel',
  'terminal': 'hooks.useKeyboardShortcuts.toggle_terminal',
}

/**
 * Read-only reference rows for the three user-rebindable panel toggles. Their
 * bindings live outside DEFAULT_SHORTCUTS (they are user-configurable and may be
 * unbound), so the caps reflect the live binding — or a muted "not set" when the
 * user has cleared it. Editing happens in Settings → Shortcuts.
 */
export function PanelToggleRows() {
  const { bindings } = usePanelToggleShortcuts()
  // Reactive, not a one-shot read: the enabled flag resolves from a config probe,
  // so a static read leaves the terminal row rendered from a stale value until
  // something else re-renders this surface. Mirrors SidePanel / EditableCodeBlock.
  const terminalEnabled = useTerminalEnabled()
  return (
    <>
      {PANEL_TOGGLE_IDS.filter(id => id !== 'terminal' || terminalEnabled).map(id => {
        const chord = bindings[id]
        return (
          <div key={id} className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
            {/* A div, not a span: the label and the "Not set" state are separate
                catalog units; a block label ends the inline text run so the i18n
                render gate never sees them joined into one string. */}
            <div className="text-[13px] text-text">{i18nT(PANEL_TOGGLE_LABEL_KEY[id])}</div>
            {chord
              ? <span className="flex items-center gap-1"><KeyCapSequence caps={formatChordKeys(chord)} plus /></span>
              : <div className="text-muted text-[11px]">{i18nT('components.shortcutsModal.unset')}</div>}
          </div>
        )
      })}
    </>
  )
}

/**
 * Desktop-only reference row for the system-wide summon hotkey. Lives outside
 * DEFAULT_SHORTCUTS because it is not a renderer chord at all: the desktop
 * shell's main process registers it OS-wide (electron/global-hotkey.js) and it
 * works while the app is in the background. Renders the accelerator as
 * ACTUALLY bound — {@link useGlobalHotkey} returns null in a plain browser and
 * when nothing could be bound, and the whole row is hidden rather than
 * advertising a chord that does not work.
 */
export function GlobalHotkeyRow() {
  const hotkey = useGlobalHotkey()
  if (!hotkey) return null
  return (
    <ShortcutRow
      label={i18nT('components.shortcutsModal.show_or_focus_the_kiro_crew_window')}
      keys={formatAcceleratorKeys(hotkey.accelerator, IS_MAC)}
    />
  )
}

export default function ShortcutsModal({ onClose }: { onClose: () => void }) {
  const { enabled, macCtrl, toggle, toggleMacCtrl } = useShortcutPrefs()
  const globalHotkey = useGlobalHotkey()

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    // Backdrop click-to-dismiss is a supplementary mouse affordance; keyboard
    // users close via Escape, already wired through the document keydown
    // listener above, so the dialog role stays keyboard-accessible.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label={i18nT('components.shortcutsModal.keyboard_shortcuts')} onClick={onClose}>
      {/* onClick only stops propagation so inner clicks don't hit the backdrop
          dismiss handler; it is event plumbing, not an interactive control. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div className="bg-card border border-border rounded-xl p-6 max-w-lg w-full mx-4 shadow-xl max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <div className="flex items-center gap-2 text-sm font-bold text-text-strong"><Keyboard size={16} /> {i18nT('components.shortcutsModal.keyboard_shortcuts_2')}</div>
          <button className="text-muted cursor-pointer hover:text-text bg-transparent border-none" onClick={onClose} aria-label={i18nT('components.shortcutsModal.close')}><X size={16} /></button>
        </div>
        {SHORTCUT_GROUPS.map(group => {
          const entries = groupShortcuts(group, macCtrl)
          if (entries.length === 0) return null
          return (
            <div key={group} className="mb-5 last:mb-0">
              <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{shortcutGroupLabel(group)}</div>
              <div className="grid gap-1">
                <ShortcutGroupRows entries={entries} hintClass="text-[11px] text-muted px-2 pb-1" />
              </div>
            </div>
          )
        })}
        <div className="mb-5 last:mb-0">
          <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('components.shortcutsModal.search')}</div>
          <div className="grid gap-1">
            <SearchEverywhereRow />
          </div>
        </div>
        <div className="mb-5 last:mb-0">
          <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('components.shortcutsModal.panel_toggles')}</div>
          <div className="grid gap-1">
            <PanelToggleRows />
          </div>
        </div>
        {globalHotkey && (
          <div className="mb-5 last:mb-0">
            <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('components.shortcutsModal.desktop_app')}</div>
            <div className="grid gap-1">
              <GlobalHotkeyRow />
            </div>
            <div className="text-[11px] text-muted mt-1 px-2">{i18nT('components.shortcutsModal.global_hotkey_hint')}</div>
          </div>
        )}
        <div className="mt-4 pt-3 border-t border-border flex items-center justify-between">
          <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
            <Toggle checked={enabled} onChange={toggle} label={i18nT('components.shortcutsModal.enable_shortcuts')} />
            <span>{i18nT('components.shortcutsModal.enable_shortcuts')}</span>
          </span>
          <span className="text-[12px] text-muted flex items-center gap-1 flex-wrap justify-end">
            {shortcutsHelpChords().map((caps, i) => (
              <span key={i} className="flex items-center gap-1">
                {i > 0 && <span className="text-[11px] mx-1">{i18nT('components.shortcutsModal.or')}</span>}
                <KeyCapSequence caps={caps} plus />
              </span>
            ))}
            {/* Block element for the same reason as the tag in ShortcutRow: it must not
                read as one string with the "or" between the chords. */}
            <div>{i18nT('components.shortcutsModal.always_works')}</div>
          </span>
        </div>
        {IS_MAC && (
          <div className="mt-2 flex items-center">
            <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
              <Toggle checked={macCtrl} onChange={toggleMacCtrl} label={i18nT('components.shortcutsModal.use_ctrl_not_option_for_chat_1_to_9')} />
              <span>{i18nT('components.shortcutsModal.use_ctrl_not_option_for_chat_1_9')}</span>
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
