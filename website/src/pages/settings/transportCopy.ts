/**
 * The badge copy for each connection method, in its own module so it can be
 * type-bound to the shared presentation mapping and unit-tested without pulling
 * in the whole settings panel.
 *
 * Two deliberate choices:
 *
 * The keys are LITERAL here rather than read off the mapping. The key-reference
 * gate (`website/scripts/check-i18n-keys.mjs`) can only check a key it resolves
 * statically, and its dynamic-site baseline is ratchet-DOWN only — its own
 * comment says never to raise it — so `i18nT(presentation.labelKey)` would buy
 * one table at the cost of two unchecked keys.
 *
 * The table is typed `Record<PresentedConnectionMethod, …>`, so adding a
 * transport to the mapping without adding its copy is a TYPE error, caught by
 * `npm run typecheck` rather than only by a test that someone has to write.
 */
import { i18nT } from '../../i18n/t'
import type { PresentedConnectionMethod } from '../../utils/remoteCrew'

/** Badge label and hover hint for one transport. Thunks, not strings: the copy
 *  has to resolve at render time, after the active language is known. */
export interface TransportCopy {
  readonly label: () => string
  readonly hint: () => string
}

/** The badges compress to acronyms (EC2 / SSM / SSH) a first-time reader may not
 *  know; the hint spells out what each one means. */
export const TRANSPORT_COPY: Record<PresentedConnectionMethod, TransportCopy> = {
  ssh: {
    label: () => i18nT('pages.settings.remoteCrewPanel.type_ssh'),
    hint: () => i18nT('pages.settings.remoteCrewPanel.transport_hint_ssh'),
  },
  ssm: {
    label: () => i18nT('pages.settings.remoteCrewPanel.type_ssm'),
    hint: () => i18nT('pages.settings.remoteCrewPanel.transport_hint_ssm'),
  },
  fargate: {
    label: () => i18nT('pages.settings.remoteCrewPanel.type_fargate'),
    hint: () => i18nT('pages.settings.remoteCrewPanel.transport_hint_fargate'),
  },
}

/** The methods this table covers, for the drift test. */
export const TRANSPORT_COPY_METHODS: readonly string[] = Object.keys(TRANSPORT_COPY)

/** Hover hint for a method this build has no copy for. The badge keeps the raw
 *  method name, so without this the row would show an unexplained machine word
 *  and a blank address; the hint says why (this build cannot connect over it)
 *  and what to do next (update, then connect again). */
export function unmappedTransportHint(method: string): string {
  return i18nT('pages.settings.remoteCrewPanel.transport_hint_unmapped', { method })
}

/** Copy for *method*, or `undefined` when this build has none — the caller
 *  renders the method's own name rather than a sibling transport's label. */
export function transportCopy(method: string): TransportCopy | undefined {
  // Own keys only: a stored method spelled like an Object.prototype member
  // (`constructor`, `__proto__`) must read as unmapped, not as inherited copy.
  if (!Object.prototype.hasOwnProperty.call(TRANSPORT_COPY, method)) return undefined
  return (TRANSPORT_COPY as Record<string, TransportCopy | undefined>)[method]
}
