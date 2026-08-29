// Pre-scan context dialog: audience/type/tone dropdowns, additional-context box,
// file path or paste input, and the Start Review button. On submit,
// POSTs to /scan and hands the job id back to the workspace so
// ScanProgress can take over.
import { X, FolderOpen } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { i18nT } from '../../../i18n/t'
import { fmtUnit } from '../../../i18n/format'
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

// AUDIENCE_I18N_KEYS / DOC_TYPE_I18N_KEYS / TONE_I18N_KEYS moved
// to ``../lib/contextOptions`` so ``SettingsPanel`` can render the same
// option catalog for the persisted defaults picker. Keeping a single
// source of truth avoids the two dialogs drifting apart on option
// membership -- a chronic risk during the incremental scanner-brief work
// where doc_type triggers were added or renamed.

// Mirror of the backend ``CONDITIONAL_SCANNERS`` map in ``__init__.py``.
// When the user picks a doc_type whose lowercased value contains any of
// these substrings, the mapped scanner is auto-checked in the picker so
// the user does not have to remember to enable it manually. The backend
// would otherwise activate the conditional scanner via its own
// ``_resolve_scanners`` substring layer, but the default settings ship
// design and email as ``false`` -- meaning the toggle silently vetoes
// the conditional activation. Auto-checking the chip is the fix: the
// user sees exactly what will run.
const CONDITIONAL_SCANNER_TRIGGERS: Readonly<Record<string, string>> = {
  design: 'design',
  email: 'email',
}

// Scanner-name -> i18n resolver imported from ``../lib/scannerNames`` so the
// New Review dialog, FindingCard, and ReviewDetail all render scanner names
// through one source of truth.

// Client-side mirror of the backend ``_MAX_DOC_TEXT_BYTES`` size cap.
// Keep the two constants aligned by number; a change on either side
// without the other creates a scan that succeeds locally and 413s
// server-side (or vice versa) and leaves users staring at cryptic
// errors. The number is stated in the UI via ``fileSizeLimitHint``
// so users know before they browse a huge PDF from their laptop.
const MAX_LOCAL_DOCUMENT_BYTES = 2 * 1024 * 1024
const MAX_LOCAL_DOCUMENT_MEGABYTES = 2

function buildPastedDocumentLabel(): string {
  // Format: ``pasted_YYYY_MM_DD_HH_MM_SS.md`` -- year-first so
  // filenames sort chronologically by name; underscore separators
  // throughout for readability at a glance. Seconds precision is
  // enough for real disambiguation (human paste-and-submit is
  // seconds-paced; nobody triggers two scans in the same second
  // outside of API-driven automation). Every character (letters,
  // digits, ``_``, ``.``) is in the backend sanitiser allowlist so
  // the persisted filename is byte-identical to what the frontend
  // displays -- no drift between the sidebar in-progress card and
  // the persisted review record.
  //
  // History: an earlier attempt used ``Intl.DateTimeFormat`` with the
  // active locale (``27/08/2026, 13:27`` for en-GB). The ``/`` chars
  // triggered the backend sanitiser's path-traversal split and
  // shredded the timestamp to things like ``2026_13_27`` (year, hour,
  // minute -- month and day silently lost). Reported by James on
  // 2026-08-27, replaced with this locale-independent shape.
  //
  // Not a locale-dependent format -- these are technical identifiers
  // for a filename, not user-facing date renderings -- so this does
  // not violate AGENTS.md's "name a locale" rule.
  const nowInstant = new Date()
  const padTwoDigits = (numberValue: number): string =>
    String(numberValue).padStart(2, '0')
  const yearComponent = String(nowInstant.getFullYear())
  const monthComponent = padTwoDigits(nowInstant.getMonth() + 1)
  const dayComponent = padTwoDigits(nowInstant.getDate())
  const hourComponent = padTwoDigits(nowInstant.getHours())
  const minuteComponent = padTwoDigits(nowInstant.getMinutes())
  const secondComponent = padTwoDigits(nowInstant.getSeconds())
  const filenameTimestamp =
    `${yearComponent}_${monthComponent}_${dayComponent}_` +
    `${hourComponent}_${minuteComponent}_${secondComponent}`
  return i18nT('apps.writingReview.newReviewDialog.pastedTimestampLabel', {
    timestamp: filenameTimestamp,
  })
}

export default function NewReviewDialog() {
  const {
    closeNewReviewDialog,
    setActiveJobId,
    setActiveJobDocName,
    settingsQuery,
  } = useWritingReview()
  const queryClient = useQueryClient()

  const [documentPath, setDocumentPath] = useState<string>('')
  const [documentText, setDocumentText] = useState<string>('')
  const [browsedFileName, setBrowsedFileName] = useState<string>('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [audienceValue, setAudienceValue] = useState<string>(
    settingsQuery.data?.default_audience ?? '',
  )
  const [docTypeValue, setDocTypeValue] = useState<string>(
    settingsQuery.data?.default_doc_type ?? '',
  )
  const [toneValue, setToneValue] = useState<string>(
    settingsQuery.data?.default_tone ?? '',
  )
  const [additionalContextText, setAdditionalContextText] = useState<string>('')
  const [askText, setAskText] = useState<string>('')
  const [submitError, setSubmitError] = useState<string>('')
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false)
  // ``null`` sentinel means "we haven't seen the server settings yet".
  // Rendering conditionally on this avoids the SettingsPanel-style race
  // where the useState initializer captured an ``undefined`` snapshot
  // and the picker stayed empty even after settings resolved. Same
  // pattern as ``SettingsPanel.tsx``'s Spock fix.
  const [scannerToggles, setScannerToggles] = useState<Record<string, boolean> | null>(null)
  // Set of scanner names the user (or a settings-panel preference) has
  // manually asserted intent over. Once a scanner is here, the
  // doc-type auto-management effect leaves it alone for the rest of
  // the dialog's lifetime. Populated in two places:
  //   1. Hydration: any conditional scanner that arrives ``true`` from
  //      settings is treated as user-managed -- a user who ticked
  //      "always run design" in Settings should not have that overridden
  //      by an unrelated doc_type pick.
  //   2. Chip clicks below: every manual toggle registers intent.
  // Kept as a ref, not state, because auto-management should NOT
  // re-run when the manual set grows -- only the doc-type change
  // should trigger the auto-toggle recomputation.
  const manuallyManagedScannersRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    // Hydrate ONLY on the first non-null resolution. Subsequent user
    // toggles inside this dialog must not be clobbered by settings
    // refetches (e.g. the settings query auto-refresh triggered by
    // React Query while the dialog is open).
    if (scannerToggles !== null) return
    const settingsToggles = settingsQuery.data?.scanner_toggles
    if (settingsToggles === undefined) return
    setScannerToggles({ ...settingsToggles })
    // Pre-populate the manual set with any conditional scanner that
    // arrived ``true`` from settings. That is a persisted preference
    // from the Settings panel and MUST survive doc_type changes.
    for (const conditionalScannerName of Object.values(CONDITIONAL_SCANNER_TRIGGERS)) {
      if (settingsToggles[conditionalScannerName]) {
        manuallyManagedScannersRef.current.add(conditionalScannerName)
      }
    }
  }, [settingsQuery.data, scannerToggles])
  useEffect(() => {
    // Auto-manage conditional scanners in response to the current
    // ``docTypeValue``. For each conditional scanner not currently in
    // the user-managed set: turn it ON when its trigger substring is
    // present in the doc_type, turn it OFF otherwise. The symmetric
    // off-flip is what fixes the bug where picking "design document"
    // then "team update" left design silently pressed -- a scan that
    // included the design scanner even though the user's final choice
    // was a non-design doc_type.
    //
    // The manual set is the escape hatch: any scanner the user
    // (or the Settings panel) has flagged as intentional stays put.
    // Auto-management is only for the "user has not touched this"
    // default case.
    if (scannerToggles === null) return
    const docTypeLower = docTypeValue.toLowerCase()
    const nextToggles = { ...scannerToggles }
    let togglesChanged = false
    for (const [triggerSubstring, targetScannerName] of Object.entries(
      CONDITIONAL_SCANNER_TRIGGERS,
    )) {
      if (!(targetScannerName in nextToggles)) continue
      if (manuallyManagedScannersRef.current.has(targetScannerName)) continue
      const shouldBeOn = docTypeLower.includes(triggerSubstring)
      if (nextToggles[targetScannerName] !== shouldBeOn) {
        nextToggles[targetScannerName] = shouldBeOn
        togglesChanged = true
      }
    }
    if (togglesChanged) {
      setScannerToggles(nextToggles)
    }
    // ``scannerToggles`` is intentionally left OUT of the dep list: we
    // only want this effect to react to doc_type changes. Including it
    // would re-run the effect every time a user manually toggled a
    // conditional chip, immediately reversing their unchecking on the
    // same doc_type. eslint-disable is scoped narrowly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docTypeValue])

  const handleBrowseFileSelected = (event: React.ChangeEvent<HTMLInputElement>) => {
    // Local-file browse: for text formats (``.md``/``.txt``) we read
    // the file with ``FileReader.readAsText`` and paste its contents
    // into ``documentText``. For binary formats (``.docx``) we route
    // through the multipart ``/uploads`` endpoint instead -- reading a
    // ZIP archive as text mangles the bytes through the JS string
    // layer's UTF-8 round-trip and downstream python-docx sees a
    // corrupted ZIP. The upload endpoint writes bytes byte-identical
    // to disk and returns a ``doc_path`` we plug into the scan
    // request; the existing path-based scan flow runs from there.
    const selectedFile = event.target.files?.[0]
    if (!selectedFile) return
    // Client-side size guard. Server enforces the same cap on the
    // upload and paste routes; catching it here saves a round-trip
    // and gives users an immediate, human-readable error instead of
    // a 413 they cannot easily interpret. Same threshold used by
    // ``handleStartReview`` for pasted text.
    if (selectedFile.size > MAX_LOCAL_DOCUMENT_BYTES) {
      setSubmitError(
        i18nT('apps.writingReview.newReviewDialog.errorFileTooLarge', {
          limit: fmtUnit(MAX_LOCAL_DOCUMENT_MEGABYTES, 'megabyte'),
        }),
      )
      setBrowsedFileName('')
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
      return
    }
    setBrowsedFileName(selectedFile.name)
    // Reset the input immediately so re-picking the same file fires
    // ``change`` again regardless of which branch we take below.
    const resetInputElement = () => {
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    }
    const lowerCasedFileName = selectedFile.name.toLowerCase()
    const shouldRouteThroughBinaryUpload = lowerCasedFileName.endsWith('.docx')
    if (shouldRouteThroughBinaryUpload) {
      // Route to the upload endpoint; clear ``documentText`` so the
      // submit path takes the ``doc_path`` branch.
      setDocumentText('')
      writingReviewApi
        .uploadDocumentFile(selectedFile)
        .then(uploadResponse => {
          setDocumentPath(uploadResponse.doc_path)
        })
        .catch(uploadError => {
          setSubmitError(
            uploadError instanceof Error
              ? uploadError.message
              : i18nT('apps.writingReview.newReviewDialog.errorGeneric'),
          )
          // Wipe the browsed filename so the user is not left with a
          // stale "browsed" hint tied to an upload that failed.
          setBrowsedFileName('')
        })
        .finally(resetInputElement)
      return
    }
    // Text-format branch: readAsText into ``documentText`` and clear
    // any stale path so the submit goes through the doc_text branch.
    setDocumentPath('')
    const fileReader = new FileReader()
    fileReader.onload = () => {
      const readResult = fileReader.result
      setDocumentText(typeof readResult === 'string' ? readResult : '')
    }
    fileReader.readAsText(selectedFile)
    resetInputElement()
  }

  const handleStartReview = async () => {
    setSubmitError('')
    // Trim all user input before validation and submit -- a trailing space on
    // the path silently fails Path(...).is_file() on the backend, and the same
    // trimming ensures context fields don't propagate stray whitespace into
    // scanner prompts.
    const documentPathTrimmed = documentPath.trim()
    const documentTextTrimmed = documentText.trim()
    const audienceTrimmed = audienceValue.trim()
    const docTypeTrimmed = docTypeValue.trim()
    const toneTrimmed = toneValue.trim()
    if (!documentPathTrimmed && !documentTextTrimmed) {
      setSubmitError(i18nT('apps.writingReview.newReviewDialog.errorInput'))
      return
    }
    // Size guard for pasted text (or the text-branch of browse, which
    // hydrated ``documentText`` from a small file). ``TextEncoder``
    // gives the UTF-8 byte count -- string ``.length`` is code
    // points and can undercount for multi-byte chars, letting a
    // fresh-encoded body sneak past a limit set in bytes. Backend
    // enforces the same limit on ``doc_text`` on the ``/scan`` route.
    // Typed ``doc_path`` values are NOT checked here because the
    // file lives on the server and the backend already caps
    // ``parse_doc`` reads at the same threshold.
    if (documentTextTrimmed) {
      const documentTextByteLength = new TextEncoder().encode(documentTextTrimmed).byteLength
      if (documentTextByteLength > MAX_LOCAL_DOCUMENT_BYTES) {
        setSubmitError(
          i18nT('apps.writingReview.newReviewDialog.errorFileTooLarge', {
            limit: fmtUnit(MAX_LOCAL_DOCUMENT_MEGABYTES, 'megabyte'),
          }),
        )
        return
      }
    }
    setIsSubmitting(true)
    try {
      const additionalContextList = additionalContextText
        .split('\n')
        .map(line => line.trim())
        .filter(line => line.length > 0)
      // Compute a distinguishable ``doc_name`` for the paste-only path
      // ONCE, at submit time, so both the payload and the sidebar
      // in-progress card share the same string. If the user browsed a
      // file, ``browsedFileName`` is authoritative and this timestamp
      // is ignored -- same for the ``doc_path`` path where the backend
      // derives the display name from the basename.
      const isPastedOnlySubmission = !browsedFileName && !documentPathTrimmed
      const pastedDocumentLabelAtSubmit = isPastedOnlySubmission
        ? buildPastedDocumentLabel()
        : ''
      const submittedDocNameForPayload = browsedFileName
        ? browsedFileName
        : isPastedOnlySubmission
          ? pastedDocumentLabelAtSubmit
          : undefined
      const scanResponse = await writingReviewApi.startScan({
        doc_path: documentPathTrimmed || undefined,
        doc_text: documentTextTrimmed || undefined,
        // Original filename for browse-uploaded docs — the backend uses this
        // (after sanitisation) for the review record's display name so the
        // sidebar shows "hapi.md" instead of the uuid storage key. For
        // paste-only submissions (no browse, no path) we substitute a
        // timestamped label like "pasted 2026-08-27 10:23" so multiple
        // pasted reviews stay distinguishable in the sidebar and review list.
        // Omitted when the user typed a ``doc_path`` (backend derives the
        // display name from the path).
        doc_name: submittedDocNameForPayload,
        // Per-scan opt-in list. Omitted entirely when we have not seen
        // settings yet -- the backend then falls back to the server-side
        // defaults from ``settings.json``, which is exactly what an
        // impatient submit before settings resolve should do.
        scanner_toggles: scannerToggles ?? undefined,
        context: {
          audience: audienceTrimmed,
          doc_type: docTypeTrimmed,
          tone: toneTrimmed,
          // Trim on the way out so a stray newline / trailing space
          // from the textarea does not become part of the LLM directive
          // line and does not persist to disk as authored whitespace.
          ask: askText.trim(),
          additional_context: additionalContextList,
        },
      })
      setActiveJobId(scanResponse.job_id)
      // Surface the doc name in the sidebar as an in-progress card until the
      // scan finishes and a real review record shows up. The three branches
      // mirror the ``submittedDocNameForPayload`` precedence: browse wins,
      // then path basename, then the timestamped paste label. The basename
      // regex accepts both POSIX (forward slash) and Windows (backslash)
      // separators — a literal string split on a single slash was rejected
      // by the cross-platform-portability CI gate because it silently drops
      // Windows separators.
      const documentPathBasename = documentPathTrimmed
        ? documentPathTrimmed.match(/[^/\\]+(?=[/\\]*$)/)?.[0] ?? documentPathTrimmed
        : ''
      const docNameForSidebar = browsedFileName
        ? browsedFileName
        : documentPathTrimmed
          ? documentPathBasename
          : pastedDocumentLabelAtSubmit
      setActiveJobDocName(docNameForSidebar)
      await queryClient.invalidateQueries({ queryKey: ['writing-review', 'reviews'] })
      closeNewReviewDialog()
    } catch (error) {
      setSubmitError(
        error instanceof Error
          ? error.message
          : i18nT('apps.writingReview.newReviewDialog.errorGeneric'),
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  const dialogRootRef = useRef<HTMLDivElement>(null)
  const requestClose = useCallback(() => closeNewReviewDialog(), [closeNewReviewDialog])
  // Escape dismissal, Tab/Shift+Tab cycling, focus-in on mount, focus-restore
  // on unmount, and the IME guard all come from the shared hook. This mirrors
  // Sage's ``AddReposModal`` / Issue Radar's ``ConnectRepoModal`` pattern for
  // app-scoped dialogs; the full-viewport ``<Modal>`` component uses the same
  // hook internally, so the two paths inherit an identical a11y contract.
  useDialogFocusTrap(dialogRootRef, requestClose)

  return (
    <div className="absolute inset-0 bg-bg/50 backdrop-blur-sm flex items-center justify-center z-10">
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions --
          ``role="dialog"`` is set on the inner div, which IS an interactive
          role per WAI-ARIA; the rule cannot see that from the ``<div>``
          element alone and treats it as non-interactive. */}
      <div
        ref={dialogRootRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="writing-review-new-dialog-title"
        tabIndex={-1}
        // The shared focus trap listens on window in capture phase. The
        // page's own keydown shortcuts must not fire while the user types
        // inside the dialog, so we stop propagation at the dialog root --
        // the capture listener still sees the event first and can act on
        // Escape/Tab before it is stopped here. Same pattern as
        // ``AddReposModal``.
        onKeyDown={event => event.stopPropagation()}
        className="w-[520px] max-w-[92%] bg-card border border-border rounded-lg shadow-lg flex flex-col max-h-[90%] outline-none"
      >
        <header className="flex items-center justify-between p-3 border-b border-border">
          <h2
            id="writing-review-new-dialog-title"
            className="text-[14px] font-medium text-text"
          >
            {i18nT('apps.writingReview.newReviewDialog.title')}
          </h2>
          <button
            type="button"
            onClick={closeNewReviewDialog}
            className="p-1 rounded hover:bg-bg-hover"
            aria-label={i18nT('apps.writingReview.newReviewDialog.close')}
          >
            <X className="lucide-inline" aria-hidden="true" />
          </button>
        </header>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
          <FieldGroup labelText={i18nT('apps.writingReview.newReviewDialog.docPath')}>
            <div className="flex items-center gap-2">
              {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- ``FieldGroup`` renders a visible caption above every input; the rule cannot see across the component boundary. */}
              <input
                type="text"
                value={documentPath}
                onChange={event => setDocumentPath(event.target.value)}
                placeholder={i18nT('apps.writingReview.newReviewDialog.docPathPlaceholder')}
                className="flex-1 min-w-0 px-2 py-1.5 rounded border border-border bg-bg text-[13px] text-text"
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1.5 rounded border border-border text-[12px] text-text hover:bg-bg-hover"
                title={i18nT('apps.writingReview.newReviewDialog.browseHint')}
              >
                <FolderOpen className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.writingReview.newReviewDialog.browse')}
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".md,.txt,.docx,text/markdown,text/plain"
                className="hidden"
                aria-hidden="true"
                onChange={handleBrowseFileSelected}
              />
            </div>
            {browsedFileName && (
              <div className="mt-1 text-[11px] text-muted">
                {i18nT('apps.writingReview.newReviewDialog.browsedHint', {
                  fileName: browsedFileName,
                })}
              </div>
            )}
            <div className="mt-1 text-[11px] text-muted">
              {i18nT('apps.writingReview.newReviewDialog.fileSizeLimitHint', {
                limit: fmtUnit(MAX_LOCAL_DOCUMENT_MEGABYTES, 'megabyte'),
              })}
            </div>
          </FieldGroup>
          <FieldGroup labelText={i18nT('apps.writingReview.newReviewDialog.docText')}>
            {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- ``FieldGroup`` renders a visible caption above every input; the rule cannot see across the component boundary. */}
            <textarea
              value={documentText}
              onChange={event => setDocumentText(event.target.value)}
              rows={6}
              placeholder={i18nT('apps.writingReview.newReviewDialog.docTextPlaceholder')}
              className="w-full px-2 py-1.5 rounded border border-border bg-bg text-[13px] text-text font-mono"
            />
          </FieldGroup>
          <SelectFieldWithOther
            labelText={i18nT('apps.writingReview.newReviewDialog.audience')}
            value={audienceValue}
            onChange={setAudienceValue}
            optionI18nKeys={AUDIENCE_I18N_KEYS}
            ariaLabel={i18nT('apps.writingReview.newReviewDialog.audience')}
          />
          <SelectFieldWithOther
            labelText={i18nT('apps.writingReview.newReviewDialog.docType')}
            value={docTypeValue}
            onChange={setDocTypeValue}
            optionI18nKeys={DOC_TYPE_I18N_KEYS}
            ariaLabel={i18nT('apps.writingReview.newReviewDialog.docType')}
          />
          <SelectFieldWithOther
            labelText={i18nT('apps.writingReview.newReviewDialog.tone')}
            value={toneValue}
            onChange={setToneValue}
            optionI18nKeys={TONE_I18N_KEYS}
            ariaLabel={i18nT('apps.writingReview.newReviewDialog.tone')}
          />
          <FieldGroup labelText={i18nT('apps.writingReview.newReviewDialog.ask')}>
            {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- ``FieldGroup`` renders a visible caption above every input; the rule cannot see across the component boundary. */}
            <textarea
              value={askText}
              onChange={event => setAskText(event.target.value)}
              rows={2}
              placeholder={i18nT('apps.writingReview.newReviewDialog.askPlaceholder')}
              className="w-full px-2 py-1.5 rounded border border-border bg-bg text-[12.5px] text-text"
            />
          </FieldGroup>
          <FieldGroup labelText={i18nT('apps.writingReview.newReviewDialog.additionalContext')}>
            {/* eslint-disable-next-line jsx-a11y/control-has-associated-label -- ``FieldGroup`` renders a visible caption above every input; the rule cannot see across the component boundary. */}
            <textarea
              value={additionalContextText}
              onChange={event => setAdditionalContextText(event.target.value)}
              rows={3}
              placeholder={i18nT('apps.writingReview.newReviewDialog.additionalContextPlaceholder')}
              className="w-full px-2 py-1.5 rounded border border-border bg-bg text-[12.5px] text-text"
            />
          </FieldGroup>
          {scannerToggles !== null && (
            <div>
              <div className="text-[11.5px] uppercase tracking-wide text-muted">
                {i18nT('apps.writingReview.newReviewDialog.scannersLabel')}
              </div>
              <div className="text-[11.5px] text-muted mt-0.5 mb-1.5">
                {i18nT('apps.writingReview.newReviewDialog.scannerToggleHelper')}
              </div>
              <div
                role="group"
                aria-label={i18nT('apps.writingReview.newReviewDialog.scannersLabel')}
                className="flex flex-wrap gap-1.5"
              >
                {Object.entries(scannerToggles).map(([scannerName, isEnabled]) => (
                  <button
                    key={scannerName}
                    type="button"
                    aria-pressed={isEnabled}
                    data-scanner-name={scannerName}
                    onClick={() => {
                      // Any click on a chip -- whether flipping OFF an
                      // auto-checked scanner or flipping ON one the
                      // auto-effect had not touched -- registers manual
                      // intent. The doc-type auto-management effect
                      // reads this ref before touching any scanner and
                      // backs off entirely for ones in the set.
                      manuallyManagedScannersRef.current.add(scannerName)
                      setScannerToggles(previousToggles =>
                        previousToggles === null
                          ? previousToggles
                          : {
                              ...previousToggles,
                              [scannerName]: !previousToggles[scannerName],
                            },
                      )
                    }}
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
            </div>
          )}
        </div>
        {submitError && (
          <div
            role="alert"
            aria-live="polite"
            className="px-4 py-2 text-[12px] text-danger border-t border-danger bg-danger-subtle"
          >
            {submitError}
          </div>
        )}
        <footer className="flex justify-end gap-2 p-3 border-t border-border">
          <button
            type="button"
            onClick={closeNewReviewDialog}
            className="px-3 py-1.5 rounded-md border border-border text-[13px] text-text hover:bg-bg-hover"
          >
            {i18nT('apps.writingReview.newReviewDialog.cancel')}
          </button>
          <button
            type="button"
            disabled={isSubmitting}
            onClick={handleStartReview}
            className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium disabled:opacity-50"
          >
            {isSubmitting
              ? i18nT('apps.writingReview.newReviewDialog.starting')
              : i18nT('apps.writingReview.newReviewDialog.start')}
          </button>
        </footer>
      </div>
    </div>
  )
}

// ``FieldGroup`` is imported from ``../lib/FieldGroup``. The local
// implementation lived here originally and was extracted alongside
// ``SettingsPanel``'s copy so both dialogs share one source of truth.

interface SelectFieldWithOtherProps {
  labelText: string
  value: string
  onChange: (nextValue: string) => void
  optionI18nKeys: Readonly<Record<string, string>>
  ariaLabel: string
}

function SelectFieldWithOther({
  labelText,
  value,
  onChange,
  optionI18nKeys,
  ariaLabel,
}: SelectFieldWithOtherProps) {
  // ``DropdownWithOther`` renders the same three-way behaviour that
  // ``SettingsPanel``'s Defaults picker uses, so a user's persisted
  // custom default (e.g. ``default_audience = "Q3 board readers"``)
  // arrives here as a free-form value, drops the picker into "Other"
  // mode, and shows the custom string in its inline text input. Picking
  // a predefined option or typing a new value emits a plain string
  // through ``onChange`` -- the wire contract is unchanged from the old
  // raw ``<select>`` implementation, so payloads for the scan endpoint
  // still carry canonical keys for predefined picks and free-form
  // strings only for user-authored values.
  const resolvedOptions = useResolvedDropdownOptions(optionI18nKeys)
  return (
    <FieldGroup labelText={labelText}>
      <DropdownWithOther
        value={value}
        onChange={onChange}
        options={resolvedOptions}
        ariaLabel={ariaLabel}
      />
    </FieldGroup>
  )
}
