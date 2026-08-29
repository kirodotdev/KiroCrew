/**
 * Contract test for ``resolveScannerName``.
 *
 * Two behaviours pinned here:
 *
 * 1. A known scanner name resolves to its localised label via i18n.
 * 2. An unknown scanner name falls back to the raw ID string, not to
 *    ``undefined``, an empty string, or a thrown error.
 *
 * The second case is the load-bearing one: a future scanner added on the
 * backend (or a scanner renamed on one side without the other) must render
 * legibly rather than silently disappearing or crashing the finding card.
 * Spock's tq-01 finding named this out as a gap; this file closes it.
 */
import { describe, it, expect } from 'vitest'

import { resolveScannerName, SCANNER_NAME_I18N_KEYS } from './scannerNames'

describe('resolveScannerName', () => {
  it('returns the resolved i18n label for a known scanner name', () => {
    // ``clarity`` is one of the always-on scanners with a live i18n key in
    // en.json under ``apps.writingReview.scannerNames.clarity``. When i18next
    // is not initialised in the test environment it returns the key itself,
    // which is still an acceptable label (dotted-path fallback rather than
    // empty). Either way, the return value is a non-empty string that differs
    // from a raw scanner-ID fallback.
    const resolvedScannerLabel = resolveScannerName('clarity')
    expect(resolvedScannerLabel).toBeTruthy()
    expect(resolvedScannerLabel).not.toBe('clarity')
  })

  it('falls back to the raw scanner-ID string for an unknown scanner name', () => {
    // A scanner name absent from ``SCANNER_NAME_I18N_KEYS`` must render as its
    // raw ID rather than ``undefined``, ``""``, or a thrown error. This is
    // the future-proofing path: a backend scanner ships before its i18n key,
    // or is renamed on the Python side but not yet on the frontend.
    const fabricatedScannerName = '__scanner_that_does_not_exist_yet__'
    expect(SCANNER_NAME_I18N_KEYS[fabricatedScannerName]).toBeUndefined()
    const resolvedScannerLabel = resolveScannerName(fabricatedScannerName)
    expect(resolvedScannerLabel).toBe(fabricatedScannerName)
  })
})
