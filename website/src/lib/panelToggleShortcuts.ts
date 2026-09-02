import { isMac } from '../utils/platform'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import {
  chordMatchesEvent,
  isValidChord,
  normalizeChord,
  type QuickSearchChord as Chord,
} from './quickSearchShortcut'

/**
 * User-rebindable shortcuts that toggle the four dashboard panels: the left
 * navigation rail, the chat session list, the right-hand activity/side panel,
 * and the docked terminal.
 *
 * The chord machinery (recording, matching, platform-neutral `mod`, key-cap
 * rendering) is shared verbatim with {@link ./quickSearchShortcut}; this module
 * only adds the multi-binding storage and the one thing quick-search never
 * needed — a real UNBOUND state, so a user can clear a toggle to nothing.
 *
 * Storage holds OVERRIDES only, never the resolved bindings: a panel absent from
 * the map falls back to its {@link DEFAULT_PANEL_TOGGLE_BINDINGS} entry (so a
 * later change to a code default reaches every user who never touched it), while
 * an explicit `null` is a deliberate "unbound" the loader must preserve rather
 * than collapse back to the default. Writers broadcast
 * {@link PANEL_TOGGLE_SHORTCUTS_EVENT} so the live keydown handler and the
 * Settings / Alt+K display surfaces re-read without a reload, mirroring the
 * quick-search preference.
 */

export type { Chord as PanelToggleChord }

/** localStorage key holding the JSON-serialized {@link PanelToggleOverrides}. */
export const PANEL_TOGGLE_SHORTCUTS_KEY = 'mc-panel-toggle-shortcuts'

/** Window event dispatched after a binding changes, so live readers refresh. */
export const PANEL_TOGGLE_SHORTCUTS_EVENT = 'mc-panel-toggle-shortcuts-changed'

export type PanelToggleId = 'left-sidebar' | 'session-panel' | 'side-panel' | 'terminal'

/** The togglable panels, in display order. */
export const PANEL_TOGGLE_IDS: readonly PanelToggleId[] = ['left-sidebar', 'session-panel', 'side-panel', 'terminal']

/**
 * Factory-default binding per panel. `mod` is the platform primary modifier —
 * Cmd on macOS, Ctrl on Windows/Linux — so the session/side defaults read as ⌘B
 * / ⌘\ on a Mac and Ctrl+B / Ctrl+\ elsewhere. The two bound defaults are
 * collision-free against the built-in chords in `useKeyboardShortcuts`.
 *
 * The left sidebar and the docked terminal both ship UNBOUND (`null`): the user
 * opts in by recording a chord in Settings.
 *
 * The terminal's reason for shipping unbound is specific, and is why no default
 * is proposed for it. Every plausible default costs a keystroke inside the shell,
 * because {@link PANEL_TOGGLES_SKIPPING_SHELL} takes its chord from the PTY by
 * design — that is the whole point of the entry. ⌘J / Ctrl+J, the obvious pick
 * (VS Code's Toggle Panel, which hosts its integrated terminal), is `^J` on
 * Windows/Linux, i.e. readline's `accept-line`: a user pressing it instead of
 * Enter would close the panel mid-command. VS Code's terminal chord proper,
 * literal `Ctrl+`` on every platform, is not expressible here (`mod` is Cmd on
 * macOS by definition) and ⌘` is the macOS window cycler. Rather than pick which
 * shell keystroke to spend on everyone's behalf, the binding is left to the user
 * who wants it — and who then knows what they traded.
 */
export const DEFAULT_PANEL_TOGGLE_BINDINGS: Record<PanelToggleId, Chord | null> = {
  'left-sidebar': null,
  'session-panel': { key: 'b', mod: true },
  'side-panel': { key: '\\', mod: true },
  'terminal': null,
}

/**
 * User overrides. A present key wins over the default — including an explicit
 * `null`, which means the user cleared that toggle to unbound. An absent key
 * falls through to {@link DEFAULT_PANEL_TOGGLE_BINDINGS}.
 */
export type PanelToggleOverrides = Partial<Record<PanelToggleId, Chord | null>>

/**
 * The panels whose chord SKIPS THE SHELL — i.e. still fires while an embedded
 * terminal holds focus, instead of being conceded to the PTY.
 *
 * Everything else yields, because a keystroke aimed at a shell belongs to the
 * shell. The terminal toggle is the exception that has to be made: opening that
 * panel focuses its own terminal, so conceding there would leave the chord able
 * to open the panel and never close it.
 *
 * This mirrors VS Code's `terminal.integrated.commandsToSkipShell` — a per-COMMAND
 * allowlist rather than a per-key rule, so the small set of workbench commands
 * that must survive terminal focus is stated as data next to the bindings it
 * qualifies. A future panel with the same need joins the set instead of adding a
 * second special case to the keydown handler.
 */
export const PANEL_TOGGLES_SKIPPING_SHELL: ReadonlySet<PanelToggleId> = new Set<PanelToggleId>(['terminal'])

function isPanelToggleId(id: string): id is PanelToggleId {
  return (PANEL_TOGGLE_IDS as readonly string[]).includes(id)
}

/**
 * Read the stored overrides, dropping any malformed entry rather than throwing.
 * A per-panel value survives only if it is `null` (unbound) or a valid chord;
 * anything else — a bad key, an unknown panel id, a non-object payload — is
 * discarded so a corrupt or hostile entry degrades to the code default rather
 * than breaking keyboard input.
 */
export function loadPanelToggleOverrides(): PanelToggleOverrides {
  const raw = safeGetItem(PANEL_TOGGLE_SHORTCUTS_KEY)
  if (!raw) return {}
  const out: PanelToggleOverrides = {}
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown> | null
    if (!parsed || typeof parsed !== 'object') return {}
    for (const [id, value] of Object.entries(parsed)) {
      if (!isPanelToggleId(id)) continue
      if (value === null) out[id] = null
      else if (isValidChord(value as Partial<Chord>)) out[id] = normalizeChord(value as Chord)
    }
  } catch {
    return {}
  }
  return out
}

/** The effective binding for a panel: an override if present (even `null`), else the default. */
export function resolvePanelToggleBinding(id: PanelToggleId, overrides: PanelToggleOverrides): Chord | null {
  return Object.prototype.hasOwnProperty.call(overrides, id)
    ? overrides[id] ?? null
    : DEFAULT_PANEL_TOGGLE_BINDINGS[id]
}

/** The effective binding for every panel — convenience for display surfaces. */
export function resolvePanelToggleBindings(overrides: PanelToggleOverrides): Record<PanelToggleId, Chord | null> {
  return {
    'left-sidebar': resolvePanelToggleBinding('left-sidebar', overrides),
    'session-panel': resolvePanelToggleBinding('session-panel', overrides),
    'side-panel': resolvePanelToggleBinding('side-panel', overrides),
    'terminal': resolvePanelToggleBinding('terminal', overrides),
  }
}

/**
 * Set one panel's binding and broadcast the change. `null` clears it to unbound;
 * a chord is normalized and stored. A non-null chord with no `mod`/`alt`
 * modifier is refused (returns false, storage untouched) — a bare-key binding
 * would fire mid-typing. Only the one panel's entry changes; the others are
 * preserved.
 */
export function setPanelToggleBinding(id: PanelToggleId, chord: Chord | null): boolean {
  if (chord !== null && !isValidChord(chord)) return false
  const overrides = loadPanelToggleOverrides()
  overrides[id] = chord === null ? null : normalizeChord(chord)
  const ok = safeSetItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify(overrides))
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(PANEL_TOGGLE_SHORTCUTS_EVENT))
  }
  return ok
}

/**
 * The panel whose resolved binding matches this keydown, or `null` for none.
 * Unbound panels never match. First match wins — there is no collision guard, so
 * if a user rebinds two panels to the same chord the earlier id in
 * {@link PANEL_TOGGLE_IDS} takes it. `mac` is injectable for testing both
 * platform behaviours, matching `chordMatchesEvent`.
 */
export function matchPanelToggleEvent(
  e: Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  overrides: PanelToggleOverrides,
  mac: boolean = isMac,
): PanelToggleId | null {
  for (const id of PANEL_TOGGLE_IDS) {
    const binding = resolvePanelToggleBinding(id, overrides)
    if (binding && chordMatchesEvent(e, binding, mac)) return id
  }
  return null
}
