import { i18nT } from '../../i18n/t'

/**
 * `permissions.browser`: what an app may do on the browser tabs of its own slots
 * (rfc-app-sdk-browser.md). Mirrors `VERBS` in `apps/browser_sdk.py`; an entry not listed here
 * grants nothing, so it is never shown as if it did.
 */
export const BROWSER_VERBS = ['view', 'read', 'act'] as const

export type BrowserVerb = (typeof BROWSER_VERBS)[number]

/** The known verbs in a declared or approved list, in the one fixed order. */
export function knownBrowserVerbs(value: unknown): BrowserVerb[] {
  return Array.isArray(value) ? BROWSER_VERBS.filter(verb => value.includes(verb)) : []
}

/**
 * The one sentence for a verb, shared by the trust prompt and the app page, so an
 * owner reads the same words wherever they see it.
 */
export function browserVerbLine(verb: BrowserVerb): string {
  switch (verb) {
    case 'view': return i18nT('components.appstore.trustAppModal.browser_verb_view')
    case 'read': return i18nT('components.appstore.trustAppModal.browser_verb_read')
    case 'act': return i18nT('components.appstore.trustAppModal.browser_verb_act')
  }
}
