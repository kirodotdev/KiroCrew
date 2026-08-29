// Shared scanner-name -> i18n resolver.
//
// Every UI surface that renders a scanner name goes through
// ``resolveScannerName`` so a single translation source covers the New Review
// dialog chip labels, the FindingCard scanner/rule line, related-location
// citations on collated findings, and the failed-scanner list in
// ReviewDetail. Without this, each surface renders the raw scanner ID string
// (``clarity``, ``naturalness``, ...) that the backend emits.
//
// The map holds FULL literal i18n keys, same pattern as
// ``AUDIENCE_I18N_KEYS`` in ``contextOptions.ts`` and ``UPDATE_ERROR_KEYS`` in
// ``pages/settings/AboutPanel.tsx``: written out per scanner rather than
// assembled via `` `apps.writingReview.scannerNames.${name}` `` so
// ``dynamicKeys.test.ts`` and ``deadKeys.test.ts`` can see them.
//
// Values here mirror the backend ``ALWAYS_ON_SCANNERS`` tuple + the values of
// ``CONDITIONAL_SCANNERS`` in
// ``src/kiro_crew/apps/builtins/writing_review/__init__.py``. Adding a scanner
// on the backend without adding it here is a runtime miss: unknown names fall
// back to the raw ID via the ``?? scannerName`` guard, which is legible but
// visibly untranslated.
import { i18nT } from '../../../i18n/t'

export const SCANNER_NAME_I18N_KEYS: Readonly<Record<string, string>> = {
  clarity: 'apps.writingReview.scannerNames.clarity',
  naturalness: 'apps.writingReview.scannerNames.naturalness',
  structure: 'apps.writingReview.scannerNames.structure',
  evidence: 'apps.writingReview.scannerNames.evidence',
  consistency: 'apps.writingReview.scannerNames.consistency',
  attribution: 'apps.writingReview.scannerNames.attribution',
  audience: 'apps.writingReview.scannerNames.audience',
  readability: 'apps.writingReview.scannerNames.readability',
  design: 'apps.writingReview.scannerNames.design',
  email: 'apps.writingReview.scannerNames.email',
}

/**
 * Resolve a scanner name to its localised display string. Unknown scanner
 * names fall back to the raw ID -- legible for debugging, and impossible to
 * hide behind i18next's silent-fallback behaviour because
 * ``dynamicKeys.test.ts`` catches a template-literal pattern that would
 * synthesise the key. A future scanner added on the backend before its i18n
 * key ships shows up as the raw ID rather than as a rendered dotted key.
 */
export function resolveScannerName(scannerName: string): string {
  const literalI18nKey = SCANNER_NAME_I18N_KEYS[scannerName]
  return literalI18nKey ? i18nT(literalI18nKey) : scannerName
}
