import { safeSetItem } from '../utils/safeStorage'
import { useEffect, useState } from 'react'
import { X, Keyboard } from 'lucide-react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, IS_MAC, MAC_CTRL_DIGITS_KEY } from '../hooks/useKeyboardShortcuts'
import { Toggle } from './ui'

/** Shortcut group headings, in display order. Shared with Settings → Shortcuts. */
export const SHORTCUT_GROUPS = ['Chat Navigation', 'Panel Navigation', 'Actions'] as const

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
 * Search Everywhere reference row. Its bindings live outside DEFAULT_SHORTCUTS:
 * the double-Shift sequence + ⌘K/Ctrl+K global trigger is wired in
 * useCommandPalette (not the Alt-based useKeyboardShortcuts handler), so it is
 * documented with this dedicated row.
 */
export function SearchEverywhereRow() {
  return (
    <div className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
      <span className="text-[13px] text-text">Search Everywhere</span>
      <span className="flex items-center gap-1">
        <Kbd>{IS_MAC ? '⇧' : 'Shift'}</Kbd>
        <Kbd>{IS_MAC ? '⇧' : 'Shift'}</Kbd>
        <span className="text-muted text-[11px] mx-1">or</span>
        <Kbd>{IS_MAC ? '⌘' : 'Ctrl'}</Kbd>
        <span className="text-muted text-[11px]">+</span>
        <Kbd>K</Kbd>
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
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onClick={onClose}>
      {/* onClick only stops propagation so inner clicks don't hit the backdrop
          dismiss handler; it is event plumbing, not an interactive control. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div className="bg-card border border-border rounded-xl p-6 max-w-lg w-full mx-4 shadow-xl max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <div className="flex items-center gap-2 text-sm font-bold text-text-strong"><Keyboard size={16} /> Keyboard Shortcuts</div>
          <button className="text-muted cursor-pointer hover:text-text bg-transparent border-none" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        {SHORTCUT_GROUPS.map(group => (
          <div key={group} className="mb-5 last:mb-0">
            <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{group}</div>
            <div className="grid gap-1">
              {groupShortcuts(group, macCtrl).map(s => (
                <ShortcutRow key={s.id} label={s.label} keys={formatShortcut(s).split(' + ')} />
              ))}
            </div>
          </div>
        ))}
        <div className="mb-5 last:mb-0">
          <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">Search</div>
          <div className="grid gap-1">
            <SearchEverywhereRow />
          </div>
        </div>
        <div className="mt-4 pt-3 border-t border-border flex items-center justify-between">
          <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
            <Toggle checked={enabled} onChange={toggle} label="Enable shortcuts" />
            <span>Enable shortcuts</span>
          </span>
          <span className="text-[12px] text-muted">
            <Kbd>{IS_MAC ? '⌥' : 'Alt'}</Kbd> <span className="text-[11px]">+</span> <Kbd>K</Kbd> always works
          </span>
        </div>
        {IS_MAC && (
          <div className="mt-2 flex items-center">
            <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
              <Toggle checked={macCtrl} onChange={toggleMacCtrl} label="Use Ctrl (not Option) for chat 1 to 9" />
              <span>Use ⌃ Ctrl (not ⌥ Option) for chat 1–9</span>
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
