/**
 * The speech provider list a capability response serves, for STT and TTS alike.
 *
 * The gateway lists every provider it accepts, built-ins first in their usual
 * order, then the ones a composed edition registered on its platform context.
 * Each entry says whether it can run now and, when it cannot, why and what the
 * user can do about it. The selectors render from this list so an edition's
 * provider shows up without a change to this file.
 *
 * An older gateway serves no list (TTS) or a list of bare names (STT). Both
 * still render: `parseSpeechProviders` returns null for the first, so the panel
 * keeps its own hardcoded options, and reads a bare name as an available
 * built-in for the second.
 */

/** What the user can do to make an unavailable provider run. */
export interface SpeechProviderAction {
  kind: 'login'
  /** The account to sign in to, as the provider names it ("Acme account"). */
  label: string
}

export interface SpeechProviderEntry {
  /** Config value, e.g. `local` or `acme`. */
  name: string
  /** Display label from the provider. Built-ins are labelled from the catalog instead. */
  label: string
  builtin: boolean
  available: boolean
  /** Why it cannot run now; null when it can or the provider gave no reason. */
  reason: string | null
  action: SpeechProviderAction | null
  /**
   * True only when the provider's check timed out or raised: a failure, not a
   * missing prerequisite. Missing (an older gateway) reads as false.
   */
  error: boolean
}

function parseAction(raw: unknown): SpeechProviderAction | null {
  if (!raw || typeof raw !== 'object') return null
  const { kind, label } = raw as Record<string, unknown>
  // Only `login` is defined. An unknown kind is dropped, not rendered as a
  // sign-in hint it may not be.
  if (kind !== 'login' || typeof label !== 'string' || !label) return null
  return { kind, label }
}

function parseEntry(raw: unknown): SpeechProviderEntry | null {
  if (typeof raw === 'string') {
    // The STT response before entries carried objects: built-in names only.
    return raw ? { name: raw, label: '', builtin: true, available: true, reason: null, action: null, error: false } : null
  }
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (typeof r.name !== 'string' || !r.name) return null
  return {
    name: r.name,
    label: typeof r.label === 'string' ? r.label : '',
    builtin: r.builtin === true,
    // Missing means available: a provider the gateway lists is accepted, and
    // a false here would show a reason the gateway never sent.
    available: r.available !== false,
    reason: typeof r.reason === 'string' && r.reason ? r.reason : null,
    action: parseAction(r.action),
    error: r.error === true,
  }
}

/**
 * The served provider list, or null when there is none to render from.
 *
 * Malformed entries are dropped one by one, so one bad row from an edition
 * does not take the whole selector back to the fallback. A list with no usable
 * entry is null, like a missing one.
 */
export function parseSpeechProviders(raw: unknown): SpeechProviderEntry[] | null {
  if (!Array.isArray(raw)) return null
  const seen = new Set<string>()
  const entries: SpeechProviderEntry[] = []
  for (const item of raw) {
    const entry = parseEntry(item)
    if (!entry || seen.has(entry.name)) continue
    seen.add(entry.name)
    entries.push(entry)
  }
  return entries.length ? entries : null
}

/**
 * Selector options from a served list, or from the fallback names.
 *
 * `builtinLabel` labels a built-in from the catalog, so it stays translated.
 * A registered provider's label is the provider's own text, which no catalog
 * can translate; its name stands in when it sent none.
 */
export function speechProviderOptions(
  served: SpeechProviderEntry[] | null,
  fallback: readonly string[],
  builtinLabel: (name: string) => string,
): { options: string[]; labels: string[] } {
  if (!served) return { options: [...fallback], labels: fallback.map(builtinLabel) }
  return {
    options: served.map(p => p.name),
    labels: served.map(p => (p.builtin ? builtinLabel(p.name) : p.label || p.name)),
  }
}

/**
 * The entry whose status note shows under the selector, or null for none.
 *
 * Only a registered provider that cannot run gets one. Built-ins keep the
 * availability UI each panel already has, so a standalone install shows no new
 * text.
 */
export function speechProviderNotice(
  served: SpeechProviderEntry[] | null,
  selected: string,
): SpeechProviderEntry | null {
  const entry = served?.find(p => p.name === selected)
  return entry && !entry.builtin && !entry.available ? entry : null
}
