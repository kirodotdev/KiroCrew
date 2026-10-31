/**
 * The reader's own chords for app editor bindings, set in Settings → Shortcuts.
 *
 * The same shape as the panel-toggle shortcuts (`lib/panelToggleShortcuts.ts`):
 * storage holds OVERRIDES only, keyed `<app>:<binding id>`. A canonical trigger
 * replaces the manifest's, `null` unbinds the binding, and an absent key falls
 * through to what the app declares, so a later manifest change reaches every
 * reader who never touched it. Writers broadcast
 * {@link EDITOR_BINDING_OVERRIDES_EVENT} so the editor and Settings re-read
 * without a reload.
 *
 * A stored trigger is re-validated on every read with the rules a manifest's
 * trigger meets, so a corrupt or hand-edited entry degrades to the app's default
 * rather than binding Save or a bare letter.
 */
import { safeGetItem } from '../utils/safeStorage'
import { savePrefExplicitly } from '../lib/uiPrefs'
import { isMac } from '../utils/platform'
import { dashboardShortcutFor, parseCanonicalTrigger } from './editorBindingTriggers'
import type { ContributedEditorBinding } from './editorBindingContributions'

/** localStorage key holding the JSON-serialized {@link EditorBindingOverrides}. */
export const EDITOR_BINDING_OVERRIDES_KEY = 'mc-editor-binding-overrides'

/** Window event dispatched after an override changes, so live readers refresh. */
export const EDITOR_BINDING_OVERRIDES_EVENT = 'mc-editor-binding-overrides-changed'

/** `<app>:<binding id>` to a canonical trigger, or `null` for unbound. */
export type EditorBindingOverrides = Partial<Record<string, string | null>>

/** The override key for one binding. */
export function overrideKey(app: string, id: string): string {
  return `${app}:${id}`
}

/** Read the stored overrides, dropping any entry that is not `null` or a bindable trigger. */
export function loadEditorBindingOverrides(): EditorBindingOverrides {
  const raw = safeGetItem(EDITOR_BINDING_OVERRIDES_KEY)
  if (!raw) return {}
  const out: EditorBindingOverrides = {}
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!/^[^:]+:[^:]+$/.test(key)) continue
      if (value === null) out[key] = null
      // A chord a dashboard shortcut holds is dropped: the binding keeps its own default.
      else if (typeof value === 'string' && parseCanonicalTrigger(value) && dashboardShortcutFor(value, isMac) === null) out[key] = value
    }
  } catch {
    return {}
  }
  return out
}

/**
 * The bindings with the reader's chords applied: an overridden binding takes the
 * stored trigger, an unbound one is left out, the rest keep the manifest's.
 */
export function applyEditorBindingOverrides(
  bindings: readonly ContributedEditorBinding[],
  overrides: EditorBindingOverrides,
): ContributedEditorBinding[] {
  const out: ContributedEditorBinding[] = []
  for (const binding of bindings) {
    const key = overrideKey(binding.app, binding.id)
    if (!Object.prototype.hasOwnProperty.call(overrides, key)) {
      out.push(binding)
      continue
    }
    const value = overrides[key]
    if (value === null || value === undefined) continue
    const trigger = parseCanonicalTrigger(value)
    out.push(trigger ? { ...binding, trigger } : binding)
  }
  return out
}

function write(overrides: EditorBindingOverrides): boolean {
  const ok = savePrefExplicitly(EDITOR_BINDING_OVERRIDES_KEY, JSON.stringify(overrides))
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(EDITOR_BINDING_OVERRIDES_EVENT))
  return ok
}

/**
 * Set one binding's chord: a canonical trigger, or `null` to unbind it. A trigger
 * that is not bindable (Save, quick search, a bare letter) is refused and storage
 * is left as it was.
 */
export function setEditorBindingOverride(app: string, id: string, canonical: string | null): boolean {
  if (canonical !== null && !parseCanonicalTrigger(canonical)) return false
  const overrides = loadEditorBindingOverrides()
  overrides[overrideKey(app, id)] = canonical
  return write(overrides)
}

/** Drop the reader's chord for one binding, back to the app's own. */
export function resetEditorBindingOverride(app: string, id: string): boolean {
  const overrides = loadEditorBindingOverrides()
  delete overrides[overrideKey(app, id)]
  return write(overrides)
}
