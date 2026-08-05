import { safeSetItem } from '../utils/safeStorage'
import { useEffect, useState } from 'react'
import { X, Keyboard } from 'lucide-react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUT_GROUPS, shortcutGroupLabel, shortcutLabel, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, IS_MAC, MAC_CTRL_DIGITS_KEY } from '../hooks/useKeyboardShortcuts'
import { useQuickSearchShortcut } from '../hooks/useQuickSearchShortcut'
import { formatQuickSearchKeys } from '../lib/quickSearchShortcut'
import { isElectron } from '../lib/electron'
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

/** Shortcuts in `group`, with the Mac Ctrl/Option digit display adjustment applied. */
export function groupShortcuts(group: string, macCtrl: boolean) {
  // The Instances chord (⌘/Ctrl+digit) only works in the Electron shell — in a
  // plain browser those chords are reserved for browser tab switching and the
  // handler never binds (see useInstanceShortcuts). Don't advertise a binding
  // the host environment will steal.
  if (group === 'remote-crews' && !isElectron) return []
  return DEFAULT_SHORTCUTS.filter(s => s.group === group).map(s => {
    // When Mac user toggles back to Alt+digit, adjust the display
    if (IS_MAC && !macCtrl && s.id.startsWith('chat-') && s.ctrl) {
      return { ...s, ctrl: false, alt: true }
    }
    return s
  })
}

/** One reference row: label left, key caps right. */
export function ShortcutRow({ label, keys }: { label: string; keys: string[] }) {
  return (
    <div className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
      <span className="text-[13px] text-text">{label}</span>
      <span className="flex items-center gap-1">{keys.map((p, i) => <span key={i} className="flex items-center gap-1">{i > 0 && <span className="text-muted text-[11px]">+</span>}<Kbd>{p}</Kbd></span>)}</span>
    </div>
  )
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

export default function ShortcutsModal({ onClose }: { onClose: () => void }) {
  const { enabled, macCtrl, toggle, toggleMacCtrl } = useShortcutPrefs()

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
                {entries.map(s => (
                  <ShortcutRow key={s.id} label={shortcutLabel(s)} keys={formatShortcut(s).split(' + ')} />
                ))}
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
        <div className="mt-4 pt-3 border-t border-border flex items-center justify-between">
          <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
            <Toggle checked={enabled} onChange={toggle} label={i18nT('components.shortcutsModal.enable_shortcuts')} />
            <span>{i18nT('components.shortcutsModal.enable_shortcuts')}</span>
          </span>
          <span className="text-[12px] text-muted">
            <Kbd>{IS_MAC ? '⌥' : 'Alt'}</Kbd> <span className="text-[11px]">+</span> <Kbd>{i18nT('components.shortcutsModal.k')}</Kbd> {i18nT('components.shortcutsModal.always_works')}
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
