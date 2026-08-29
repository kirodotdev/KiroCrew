// Shared option-key catalog for the audience / doc-type / tone dropdowns.
//
// Both ``NewReviewDialog`` (per-scan context) and ``SettingsPanel`` (persisted
// defaults) render these lists via ``DropdownWithOther`` -- see
// ``../components/DropdownWithOther.tsx`` for the "Other (custom)..."
// contract that lets a user set values outside the catalog. Keys are the
// canonical wire values persisted on the review record and passed into the
// scanner prompt; labels come from i18n so downstream translations flow
// through automatically.
//
// A custom value flows through as a free-form string in the same field --
// the scanner prompt uses ``context.audience or 'not specified'`` and never
// looks values up in an enum, so an entry like "Q3 board deck reviewers"
// reaches the LLM verbatim. Conditional-scanner triggers in the backend
// (``CONDITIONAL_SCANNERS`` in ``__init__.py``) substring-match on the
// doc_type lower-case: a custom doc_type simply doesn't fire any conditional
// scanner, which is the correct default.
//
// ## Why the maps hold FULL literal i18n keys
//
// The earlier shape passed ``(optionKeys, optionKeyPrefix)`` to the hook and
// assembled the key with a template literal (`` `${prefix}.${key}` ``). That
// hid every option's catalog key from ``dynamicKeys.test.ts`` (the assembly is
// invisible) and from ``deadKeys.test.ts`` (a key referenced only via
// assembly is reported dead, and a pruning pass would delete it -- exactly
// the failure ``AboutPanel.tsx``'s ``UPDATE_ERROR_KEYS`` comment records).
//
// Writing each key out in full as a plain string literal makes it findable by
// every static tool: extractor, dead-key scan, and IDE go-to-definition all
// walk the source and see the literal. Same pattern as ``AboutPanel``'s
// ``UPDATE_ERROR_KEYS`` and ``McpToolsPanel``'s ``STATUS_LABEL_KEY``.
import { useMemo } from 'react'

import { i18nT } from '../../../i18n/t'
import { useLanguage } from '../../../i18n/LanguageProvider'
import type { DropdownOption } from '../components/DropdownWithOther'

/**
 * Wire-value -> full literal i18n key, one entry per option the audience
 * dropdown offers. Wire values are what get persisted to
 * ``settings.default_audience`` and passed to the backend scanner prompt.
 */
export const AUDIENCE_I18N_KEYS = {
  internalTeam: 'apps.writingReview.newReviewDialog.audienceOption.internalTeam',
  vpLeadership: 'apps.writingReview.newReviewDialog.audienceOption.vpLeadership',
  externalCustomer: 'apps.writingReview.newReviewDialog.audienceOption.externalCustomer',
  newHire: 'apps.writingReview.newReviewDialog.audienceOption.newHire',
  crossOrg: 'apps.writingReview.newReviewDialog.audienceOption.crossOrg',
} as const

/**
 * Wire-value -> full literal i18n key for the doc-type dropdown. Wire values
 * feed the backend ``CONDITIONAL_SCANNERS`` substring matcher
 * (see ``__init__.py``): a lower-cased match on ``design`` triggers the
 * design scanner, ``email`` triggers the email scanner. Rename with care --
 * the substring contract is upstream of this map.
 */
export const DOC_TYPE_I18N_KEYS = {
  decisionDoc: 'apps.writingReview.newReviewDialog.docTypeOption.decisionDoc',
  designDocument: 'apps.writingReview.newReviewDialog.docTypeOption.designDocument',
  email: 'apps.writingReview.newReviewDialog.docTypeOption.email',
  programUpdate: 'apps.writingReview.newReviewDialog.docTypeOption.programUpdate',
  strategy: 'apps.writingReview.newReviewDialog.docTypeOption.strategy',
  teamUpdate: 'apps.writingReview.newReviewDialog.docTypeOption.teamUpdate',
  investigation: 'apps.writingReview.newReviewDialog.docTypeOption.investigation',
} as const

/**
 * Wire-value -> full literal i18n key for the tone dropdown.
 */
export const TONE_I18N_KEYS = {
  neutralProfessional: 'apps.writingReview.newReviewDialog.toneOption.neutralProfessional',
  conciseExecutive: 'apps.writingReview.newReviewDialog.toneOption.conciseExecutive',
  instructional: 'apps.writingReview.newReviewDialog.toneOption.instructional',
  persuasive: 'apps.writingReview.newReviewDialog.toneOption.persuasive',
} as const

/**
 * Resolve a wire-value -> i18n-key map into the ``{ value, label }`` shape
 * ``DropdownWithOther`` expects. Memoised on the passed-in map so the
 * returned array is stable across renders -- important because
 * ``DropdownWithOther`` compares ``value`` against ``options`` on every
 * render and a new array identity would still make an unchanged option list.
 * The dependency captures the input map by identity, which is safe: the
 * exported ``*_I18N_KEYS`` maps above are module-level singletons.
 */
export function useResolvedDropdownOptions(
  optionI18nKeys: Readonly<Record<string, string>>,
): DropdownOption[] {
  // Track the resolved language so ``useMemo`` recomputes when the user
  // switches locales mid-session. ``LanguageProvider`` triggers a
  // subtree re-render via ``cloneElement(children)`` on language change,
  // but ``useMemo`` still short-circuits if its deps look identical --
  // ``optionI18nKeys`` is a stable reference, so without a language dep
  // here the ``i18nT`` labels stay frozen at the first-render locale.
  // ``useLanguage`` degrades gracefully to a read-only view outside a
  // ``LanguageProvider`` (test setups), returning a stable value that
  // keeps memoisation working there.
  const { resolved: activeResolvedLanguage } = useLanguage()
  return useMemo(
    () =>
      Object.entries(optionI18nKeys).map(([wireValue, fullI18nKey]) => ({
        value: wireValue,
        label: i18nT(fullI18nKey),
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ``activeResolvedLanguage`` is intentionally in the deps to force re-derive on locale swap. Not strictly required for the map body's data inputs, but the ``i18nT()`` calls inside would otherwise short-circuit on unchanged option keys. F6 fix -- see the F6 addendum in the app handover.
    [optionI18nKeys, activeResolvedLanguage],
  )
}
