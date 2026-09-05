/**
 * Where an agent template came from, as one word a person can act on.
 *
 * Standalone so the template dropdown and the Agent Template pane answer the
 * question identically — they sit two lines apart, so a disagreement between
 * them is visible in one glance.
 */

import { i18nT } from '../i18n/t'

/** The provenance fields this module reads, structurally — both pages declare
 *  their own `InstalledAgent`, so neither type is imported here. */
export interface TemplateProvenance {
  source?: string
  package?: string
  kirocrew_owned?: boolean
}

/**
 * `custom` is deliberately the fallback rather than a positive claim.
 *
 * Provenance is inferred from the filename, and `~/.kiro/agents` is shared with
 * other tools, so a spec an IDE plugin dropped there is indistinguishable from
 * one the user wrote. `custom` describes the template's relation to the shipped
 * set — which is verifiable — instead of naming an author, which is not.
 */
export type TemplateSourceKind = 'builtin' | 'package' | 'custom'

export function templateSourceKind(a: TemplateProvenance | undefined): TemplateSourceKind {
  // Ownership first: Kiro Crew rewrites the files it owns unconditionally, so
  // that fact outranks a filename that also happens to look package-shaped.
  if (a?.kirocrew_owned === true || a?.source === 'kirocrew') return 'builtin'
  if (a?.source === 'package') return 'package'
  return 'custom'
}

/**
 * Localised label, or '' when there is nothing worth saying.
 *
 * Keys are inline literals at each `i18nT()` call because that is the form
 * `scripts/check-i18n-keys.mjs` resolves statically, and the call happens per
 * render so a language switch re-resolves it.
 */
export function templateSourceLabel(a: TemplateProvenance | undefined): string {
  // No record at all means the template is not in the installed list — a dangling
  // reference to one that was removed. Nothing is known about it, so claim nothing.
  if (!a) return ''
  const kind = templateSourceKind(a)
  if (kind === 'builtin') return i18nT('lib.templateSource.built_in')
  // The category word. The badge shows the specific package name instead
  // (templateSourceBadge); this stays the category so the page's dedicated
  // "Package" row can carry the name without the Source row repeating it.
  if (kind === 'package') return i18nT('lib.templateSource.package')
  return i18nT('lib.templateSource.custom')
}

/**
 * What a source BADGE shows: the specific package name ("papyrus",
 * "oncall-radar") for a package template, and the category word otherwise.
 *
 * A badge is a glance with no room for a second field, so it names the package
 * directly. The page's detail section, which has a separate Package row, uses
 * templateSourceLabel instead so the two rows do not say the same thing twice.
 */
export function templateSourceBadge(a: TemplateProvenance | undefined): string {
  if (templateSourceKind(a) === 'package') {
    const pkg = a?.package?.trim()
    if (pkg) return pkg
  }
  return templateSourceLabel(a)
}
