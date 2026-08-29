// Settings panel for the Writing Review app.
//
// Surfaces three groups of persistent user settings:
//
// * Defaults for new reviews: default audience / document type / tone,
//   rendered via ``DropdownWithOther`` so a user can either pick a
//   built-in option or type a free-form custom value that persists as
//   their default. Values reach the scan pipeline verbatim -- the
//   scanner prompt uses raw string pass-through, so a custom entry like
//   "Q3 board deck reviewers" appears in the LLM prompt exactly as
//   typed.
//
// * Scanners: chip toggles mirroring ``NewReviewDialog``'s picker so a
//   user can turn scanners off by default across every scan. Per-scan
//   overrides in ``NewReviewDialog`` remain per-scan; this panel writes
//   ``scanner_toggles`` to the persisted ``settings.json`` and seeds
//   the dialog on next open.
//
// * ``max_concurrent`` -- the ceiling on how many scanner sessions run
//   in parallel per review. Backend clamps to ``[1, 9]`` (matching
//   the concurrency ceiling in ``pool.py``) and the input mirrors that
//   range so the frontend never sends a value the backend will silently
//   reject.
//
// Save flow: one form -> one PATCH request. All fields ride the same
// mutation; the backend ``update_settings`` merges the payload into the
// on-disk state via the atomic write in ``store.py`` and returns the
// full updated settings so React Query can splice into the cache without
// a follow-up GET.
import { X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { i18nT } from '../../../i18n/t'
import { useDialogFocusTrap } from '../../../hooks/useDialogFocusTrap'
import { useWritingReview } from '../context'
import { writingReviewApi } from '../api'
import { DropdownWithOther } from './DropdownWithOther'
import { FieldGroup } from '../lib/FieldGroup'
import {
  AUDIENCE_I18N_KEYS,
  DOC_TYPE_I18N_KEYS,
  TONE_I18N_KEYS,
  useResolvedDropdownOptions,
} from '../lib/contextOptions'
import { resolveScannerName } from '../lib/scannerNames'

const MAX_CONCURRENT_MINIMUM = 1
// Ceiling is 9 -- the maximum parallel scanner wave. 8 always-on scanners
// (clarity, naturalness, structure, evidence, consistency, attribution,
// audience, readability) plus at most one conditional (design XOR email).
// The concurrent-scan guard blocks a second scan from starting while one
// is in flight, so no configuration would ever need more than one wave.
const MAX_CONCURRENT_MAXIMUM = 9

export default function SettingsPanel() {
  const { closeSettingsDialog, settingsQuery } = useWritingReview()
  const queryClient = useQueryClient()

  // ``null`` on each draft field = "we haven't seen the server value
  // yet". Distinguishes "loading" from "loaded and value is empty",
  // which matters because a premature default (empty string / seeded
  // dict) would let the user Save a synthetic value before the real
  // one arrived. The hydration effect below syncs on the first non-null
  // resolution and never overwrites subsequent user edits.
  const [maxConcurrentDraft, setMaxConcurrentDraft] = useState<number | null>(null)
  const [defaultAudienceDraft, setDefaultAudienceDraft] = useState<string | null>(null)
  const [defaultDocTypeDraft, setDefaultDocTypeDraft] = useState<string | null>(null)
  const [defaultToneDraft, setDefaultToneDraft] = useState<string | null>(null)
  const [scannerTogglesDraft, setScannerTogglesDraft] = useState<
    Record<string, boolean> | null
  >(null)
  const [saveError, setSaveError] = useState<string>('')
  const [isSaving, setIsSaving] = useState<boolean>(false)

  useEffect(() => {
    // Seed drafts from server data the first time it arrives. Once any
    // field has been seeded (draft is non-null), subsequent settings-
    // query updates (e.g. from another tab, or React Query auto-refetch
    // while this panel is open) MUST NOT overwrite in-progress edits.
    // Each field guards independently: if a fresh settings response
    // arrives before the user has interacted with, say, ``defaultTone``,
    // we still hydrate that specific field. In practice the first
    // resolution seeds all fields at once so this per-field check acts
    // as belt-and-braces against a partial payload.
    const resolvedSettings = settingsQuery.data
    if (resolvedSettings === undefined) return
    if (maxConcurrentDraft === null) {
      setMaxConcurrentDraft(resolvedSettings.max_concurrent)
    }
    if (defaultAudienceDraft === null) {
      setDefaultAudienceDraft(resolvedSettings.default_audience ?? '')
    }
    if (defaultDocTypeDraft === null) {
      setDefaultDocTypeDraft(resolvedSettings.default_doc_type ?? '')
    }
    if (defaultToneDraft === null) {
      setDefaultToneDraft(resolvedSettings.default_tone ?? '')
    }
    if (scannerTogglesDraft === null) {
      // Symmetric fallback with the other four fields: hydrate to an
      // empty object if the server response omits ``scanner_toggles``
      // rather than staying ``null`` forever. Without this, a partial
      // response (or a future breaking change on the wire) would leave
      // ``anyDraftUnresolved`` permanently true and Save locked.
      // Empty toggles render as no chips, which is honest and
      // recoverable; a locked Save is not.
      setScannerTogglesDraft({ ...(resolvedSettings.scanner_toggles ?? {}) })
    }
  }, [
    settingsQuery.data,
    maxConcurrentDraft,
    defaultAudienceDraft,
    defaultDocTypeDraft,
    defaultToneDraft,
    scannerTogglesDraft,
  ])

  const clampMaxConcurrent = (rawValue: number): number => {
    // Clamp on the way in so a paste of "999" doesn't sit in state as an
    // out-of-range number the user then has to correct. The backend
    // clamps too -- this is belt-and-braces plus an immediately-visible
    // input value that matches what will be saved.
    if (Number.isNaN(rawValue)) return MAX_CONCURRENT_MINIMUM
    return Math.max(
      MAX_CONCURRENT_MINIMUM,
      Math.min(MAX_CONCURRENT_MAXIMUM, Math.trunc(rawValue)),
    )
  }

  const handleSaveSettings = async () => {
    // Guarded by the disabled Save button while any draft is still null
    // (settings query hasn't resolved). Refuse defensively as well --
    // saving with an unseen value would silently overwrite the server
    // with an incomplete payload.
    if (
      maxConcurrentDraft === null ||
      defaultAudienceDraft === null ||
      defaultDocTypeDraft === null ||
      defaultToneDraft === null ||
      scannerTogglesDraft === null
    ) {
      return
    }
    setSaveError('')
    setIsSaving(true)
    try {
      await writingReviewApi.updateSettings({
        max_concurrent: clampMaxConcurrent(maxConcurrentDraft),
        default_audience: defaultAudienceDraft.trim(),
        default_doc_type: defaultDocTypeDraft.trim(),
        default_tone: defaultToneDraft.trim(),
        scanner_toggles: scannerTogglesDraft,
      })
      await queryClient.invalidateQueries({
        queryKey: ['writing-review', 'settings'],
      })
      closeSettingsDialog()
    } catch (error) {
      setSaveError(
        error instanceof Error
          ? error.message
          : i18nT('apps.writingReview.settingsPanel.errorGeneric'),
      )
    } finally {
      setIsSaving(false)
    }
  }

  const dialogRootRef = useRef<HTMLDivElement>(null)
  const requestClose = useCallback(() => closeSettingsDialog(), [closeSettingsDialog])
  // Escape dismissal, Tab/Shift+Tab cycling, focus-in on mount,
  // focus-restore on unmount, and the IME guard all come from the shared
  // hook. Same wiring as ``NewReviewDialog``; matches the wider
  // ``AddReposModal`` pattern for app-scoped dialogs.
  useDialogFocusTrap(dialogRootRef, requestClose)

  const audienceDropdownOptions = useResolvedDropdownOptions(AUDIENCE_I18N_KEYS)
  const docTypeDropdownOptions = useResolvedDropdownOptions(DOC_TYPE_I18N_KEYS)
  const toneDropdownOptions = useResolvedDropdownOptions(TONE_I18N_KEYS)

  const anyDraftUnresolved =
    maxConcurrentDraft === null ||
    defaultAudienceDraft === null ||
    defaultDocTypeDraft === null ||
    defaultToneDraft === null ||
    scannerTogglesDraft === null

  return (
    <div className="absolute inset-0 bg-bg/50 backdrop-blur-sm flex items-center justify-center z-10">
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- ``role="dialog"`` is set on the inner div, which IS an interactive role per WAI-ARIA; the rule cannot see that from the ``<div>`` element alone and treats it as non-interactive. */}
      <div
        ref={dialogRootRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="writing-review-settings-dialog-title"
        tabIndex={-1}
        // Stop keydown propagating past the dialog so the page's own
        // shortcuts do not fire while the user types inside a settings
        // field. ``useDialogFocusTrap`` listens on window in capture
        // phase and still sees Escape/Tab first.
        onKeyDown={event => event.stopPropagation()}
        className="w-[560px] max-w-[92%] bg-card border border-border rounded-lg shadow-lg flex flex-col max-h-[90%] outline-none"
      >
        <header className="flex items-center justify-between p-3 border-b border-border">
          <h2
            id="writing-review-settings-dialog-title"
            className="text-[14px] font-medium text-text"
          >
            {i18nT('apps.writingReview.settingsPanel.title')}
          </h2>
          <button
            type="button"
            onClick={closeSettingsDialog}
            className="p-1 rounded hover:bg-bg-hover"
            aria-label={i18nT('apps.writingReview.settingsPanel.close')}
          >
            <X className="lucide-inline" aria-hidden="true" />
          </button>
        </header>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
          {anyDraftUnresolved ? (
            <p className="text-[12px] text-muted">
              {i18nT('apps.writingReview.settingsPanel.loading')}
            </p>
          ) : (
            <>
              <section
                aria-labelledby="writing-review-settings-defaults-heading"
                className="space-y-3"
              >
                <div>
                  <h3
                    id="writing-review-settings-defaults-heading"
                    className="text-[12px] font-medium text-text"
                  >
                    {i18nT('apps.writingReview.settingsPanel.defaultsHeading')}
                  </h3>
                  <p className="text-[11px] text-muted mt-0.5">
                    {i18nT('apps.writingReview.settingsPanel.defaultsHint')}
                  </p>
                </div>
                <FieldGroup labelText={i18nT('apps.writingReview.settingsPanel.defaultAudience')}>
                  <DropdownWithOther
                    value={defaultAudienceDraft ?? ''}
                    onChange={setDefaultAudienceDraft}
                    options={audienceDropdownOptions}
                    ariaLabel={i18nT('apps.writingReview.settingsPanel.defaultAudience')}
                  />
                </FieldGroup>
                <FieldGroup labelText={i18nT('apps.writingReview.settingsPanel.defaultDocType')}>
                  <DropdownWithOther
                    value={defaultDocTypeDraft ?? ''}
                    onChange={setDefaultDocTypeDraft}
                    options={docTypeDropdownOptions}
                    ariaLabel={i18nT('apps.writingReview.settingsPanel.defaultDocType')}
                  />
                </FieldGroup>
                <FieldGroup labelText={i18nT('apps.writingReview.settingsPanel.defaultTone')}>
                  <DropdownWithOther
                    value={defaultToneDraft ?? ''}
                    onChange={setDefaultToneDraft}
                    options={toneDropdownOptions}
                    ariaLabel={i18nT('apps.writingReview.settingsPanel.defaultTone')}
                  />
                </FieldGroup>
              </section>
              <section
                aria-labelledby="writing-review-settings-scanners-heading"
                className="space-y-2"
              >
                <div>
                  <h3
                    id="writing-review-settings-scanners-heading"
                    className="text-[12px] font-medium text-text"
                  >
                    {i18nT('apps.writingReview.settingsPanel.scannersHeading')}
                  </h3>
                  <p className="text-[11px] text-muted mt-0.5">
                    {i18nT('apps.writingReview.settingsPanel.scannersHint')}
                  </p>
                </div>
                <div
                  role="group"
                  aria-label={i18nT('apps.writingReview.settingsPanel.scannersHeading')}
                  className="flex flex-wrap gap-1.5"
                >
                  {scannerTogglesDraft &&
                    Object.entries(scannerTogglesDraft).map(([scannerName, isEnabled]) => (
                      <button
                        key={scannerName}
                        type="button"
                        aria-pressed={isEnabled}
                        data-scanner-name={scannerName}
                        onClick={() =>
                          setScannerTogglesDraft(previousToggles =>
                            previousToggles === null
                              ? previousToggles
                              : {
                                  ...previousToggles,
                                  [scannerName]: !previousToggles[scannerName],
                                },
                          )
                        }
                        className={
                          isEnabled
                            ? 'inline-flex items-center rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12px] text-accent hover:bg-bg-hover'
                            : 'inline-flex items-center rounded-md border border-border px-2.5 py-1 text-[12px] text-muted opacity-70 hover:opacity-100 hover:bg-bg-hover'
                        }
                      >
                        {resolveScannerName(scannerName)}
                      </button>
                    ))}
                </div>
              </section>
              <section className="space-y-2">
                {/* eslint-disable-next-line jsx-a11y/label-has-for -- Label is paired with its input two lines below via ``htmlFor``+``id``; the rule requires BOTH nesting AND htmlFor by default, which is stricter than the codebase-wide convention for this ratchet. */}
                <label
                  htmlFor="writing-review-max-concurrent"
                  className="block text-[12px] font-medium text-text"
                >
                  {i18nT('apps.writingReview.settingsPanel.maxConcurrentLabel')}
                </label>
                {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- The ``<label htmlFor="writing-review-max-concurrent">`` above IS the associated label; the rule fires alongside the ``label-has-for`` complaint because the strict default wants nesting too. */}
                <input
                  id="writing-review-max-concurrent"
                  type="number"
                  min={MAX_CONCURRENT_MINIMUM}
                  max={MAX_CONCURRENT_MAXIMUM}
                  step={1}
                  value={maxConcurrentDraft ?? ''}
                  onChange={event =>
                    setMaxConcurrentDraft(clampMaxConcurrent(Number(event.target.value)))
                  }
                  className="w-24 px-2 py-1.5 rounded border border-border bg-bg text-[13px] text-text"
                />
                <p className="text-[11px] text-muted">
                  {i18nT('apps.writingReview.settingsPanel.maxConcurrentHint', {
                    min: MAX_CONCURRENT_MINIMUM,
                    max: MAX_CONCURRENT_MAXIMUM,
                  })}
                </p>
              </section>
            </>
          )}
          {saveError && (
            <p className="text-[12px] text-danger" role="alert">
              {saveError}
            </p>
          )}
        </div>
        <footer className="flex items-center justify-end gap-2 p-3 border-t border-border">
          <button
            type="button"
            onClick={closeSettingsDialog}
            className="px-3 py-1.5 rounded border border-border text-[12px] text-text hover:bg-bg-hover"
          >
            {i18nT('apps.writingReview.settingsPanel.cancel')}
          </button>
          <button
            type="button"
            onClick={handleSaveSettings}
            disabled={isSaving || anyDraftUnresolved}
            className="px-3 py-1.5 rounded bg-accent text-accent-fg text-[12px] font-medium hover:opacity-90 disabled:opacity-60"
          >
            {isSaving
              ? i18nT('apps.writingReview.settingsPanel.saving')
              : i18nT('apps.writingReview.settingsPanel.save')}
          </button>
        </footer>
      </div>
    </div>
  )
}

// ``FieldGroup`` is imported from ``../lib/FieldGroup``. The local
// implementation lived here originally and was extracted alongside
// ``NewReviewDialog``'s copy so both dialogs share one source of truth.
