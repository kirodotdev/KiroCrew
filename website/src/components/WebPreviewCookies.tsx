import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Cookie, Upload, X, Loader2, AlertTriangle, Check, Trash2 } from 'lucide-react'

import { ApiError } from '../api/apiError'
import { fmtNumber, fmtRelative } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { useBrowserCookies } from '../hooks/useBrowserCookies'
import { DropdownMenuItem } from './ui/dropdown-menu'
import { Btn } from './ui'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'

/** The shared shape returned by `useBrowserCookies`, passed to each piece so the
 * panel holds ONE hook instance (one status read, one dialog) while the trigger,
 * chip and dialog render in their own places. */
type Cookies = ReturnType<typeof useBrowserCookies>

/** How many domains the status-chip tooltip lists before it caps with "+N more". */
const DOMAIN_TOOLTIP_CAP = 15

/** Extract the user-readable message from a failed request. `friendlyErrText`
 * (in the api layer) already unwraps a `{"error": "…"}` 400 body into
 * `ApiError.message`, so the message is the thing to show. */
function requestErrorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.message) return e.message
  if (e instanceof Error && e.message) return e.message
  return fallback
}

/** How long an armed Clear stays armed before it disarms itself. */
const CLEAR_ARM_MS = 3000

/** The domain tooltip: distinct domains, capped, with a localized "+N more". */
function domainsTooltip(domains: string[]): string {
  if (domains.length <= DOMAIN_TOOLTIP_CAP) return domains.join('\n')
  const shown = domains.slice(0, DOMAIN_TOOLTIP_CAP)
  const rest = domains.length - DOMAIN_TOOLTIP_CAP
  return `${shown.join('\n')}\n${i18nT('components.webPreviewPanel.cookies_domains_more', { count: fmtNumber(rest) })}`
}

/** The full chip line: "34 cookies · 12 sites · expires in 19h". Counts follow
 * the active locale (fmtNumber); the expiry is a relative time (fmtRelative),
 * omitted when every cookie is session-scoped. */
function cookieChipText(summary: {
  cookie_count: number
  domains: string[]
  earliest_expiry: number | null
}): string {
  const parts = [
    i18nT('components.webPreviewPanel.cookies_count', { count: fmtNumber(summary.cookie_count) }),
    i18nT('components.webPreviewPanel.cookies_sites', { count: fmtNumber(summary.domains.length) }),
  ]
  if (summary.earliest_expiry != null) {
    parts.push(i18nT('components.webPreviewPanel.cookies_expires', {
      when: fmtRelative(summary.earliest_expiry),
    }))
  }
  return parts.join(' · ')
}

/**
 * The overflow-menu entry that opens the import dialog. Lives INSIDE the preview
 * toolbar's existing "More actions" dropdown rather than as another toolbar
 * button, so the row's sibling-button count (guarded for the
 * max-two-buttons-per-row rule) does not grow. Hidden for a non-owner.
 */
export function CookieMenuItem({ cookies, onOpen }: { cookies: Cookies; onOpen: () => void }) {
  if (cookies.forbidden) return null
  return (
    <DropdownMenuItem onSelect={onOpen} data-testid="web-preview-import-cookies">
      <Cookie size={13} className="shrink-0 text-muted" />
      <span>{i18nT('components.webPreviewPanel.import_cookies')}</span>
    </DropdownMenuItem>
  )
}

/**
 * The overflow-menu entry that clears the stored set. Sits beside
 * `CookieMenuItem` in the same "More actions" dropdown, so clearing costs the
 * toolbar and the Live-view header no extra button (max-two-buttons-per-row).
 *
 * Two-step: the first select ARMS the item (the menu stays open via
 * `preventDefault`, matching `ExportSessionItem`, and the label turns into the
 * confirm wording); the second select within `CLEAR_ARM_MS` sends the DELETE.
 * The menu also stays open on the second select so the outcome — a spinner,
 * then either the item disappearing with the chip, or a failure — renders on
 * the row rather than vanishing with the menu.
 *
 * A failed DELETE renders through `ErrorNotice` (the passive alert, `askAgent`
 * off) with the hand-off as a sibling `ErrorNoticeMenuItem` whose
 * `describedBy` is the notice's `id`: inside Radix menu content a nested button
 * is skipped by the roving focus, so the hand-off has to be its own focus stop.
 *
 * Hidden for a non-owner and when nothing is imported.
 */
export function CookieClearMenuItem({ cookies }: { cookies: Cookies }) {
  const [armed, setArmed] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const armTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const errorId = useId()

  useEffect(() => () => { if (armTimer.current) clearTimeout(armTimer.current) }, [])

  const onSelect = useCallback((event: Event) => {
    // Keep the menu open: the armed state and the outcome both render here.
    event.preventDefault()
    if (cookies.clearing) return
    if (!armed) {
      setArmed(true)
      setFailure(null)
      armTimer.current = setTimeout(() => setArmed(false), CLEAR_ARM_MS)
      return
    }
    if (armTimer.current) clearTimeout(armTimer.current)
    setArmed(false)
    cookies.clear().catch((e: unknown) => {
      setFailure(requestErrorText(e, i18nT('components.webPreviewPanel.cookies_clear_failed')))
    })
  }, [armed, cookies])

  if (cookies.forbidden || !cookies.data?.present) return null

  const label = armed
    ? i18nT('components.webPreviewPanel.cookies_clear_confirm')
    : i18nT('components.webPreviewPanel.cookies_clear')

  return (
    <>
      <DropdownMenuItem
        onSelect={onSelect}
        disabled={cookies.clearing}
        className={armed ? 'text-danger' : undefined}
        data-testid="web-preview-cookies-clear"
        data-armed={armed || undefined}
      >
        {cookies.clearing
          ? <Loader2 size={13} className="shrink-0 animate-spin text-muted" />
          : <Trash2 size={13} className={`shrink-0 ${armed ? 'text-danger' : 'text-muted'}`} />}
        <span className="flex-1">{label}</span>
        {failure && (
          // The passive alert. This wrapper swallows pointer events so a click on
          // the notice does not bubble to the row and re-arm the clear.
          <span
            className="ml-auto"
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <ErrorNotice
              id={errorId}
              message={failure}
              variant="inline"
              testId="web-preview-cookies-clear-error"
            />
          </span>
        )}
      </DropdownMenuItem>
      {failure && (
        <ErrorNoticeMenuItem
          Item={DropdownMenuItem}
          message={failure}
          describedBy={errorId}
        />
      )}
    </>
  )
}

/**
 * The READ-ONLY status chip: "34 cookies · 12 sites · expires in 19h" with a
 * domain tooltip. It carries no action — Clear lives in the overflow menu
 * (`CookieClearMenuItem`) so neither the toolbar nor the Live-view header grows
 * a button. Shown only when a set is imported. `compact` drops the text to just
 * the count for the tight overlay header (full detail stays in the tooltip).
 * Hidden for a non-owner or when nothing is imported.
 */
export function CookieChip({ cookies, compact = false }: { cookies: Cookies; compact?: boolean }) {
  const summary = cookies.data?.summary ?? null
  if (cookies.forbidden || !cookies.data?.present || !summary) return null

  const label = compact
    ? i18nT('components.webPreviewPanel.cookies_chip_compact', { count: fmtNumber(summary.cookie_count) })
    : cookieChipText(summary)
  const tooltip = compact
    ? `${cookieChipText(summary)}\n${domainsTooltip(summary.domains)}`
    : domainsTooltip(summary.domains)

  return (
    <span
      className="inline-flex items-center gap-1 shrink min-w-0 max-w-[240px] h-6 px-1.5 rounded-md bg-bg-elevated border border-border text-[11px] text-muted"
      data-testid="web-preview-cookies-chip"
      title={tooltip}
    >
      <Cookie size={11} className="shrink-0 text-accent" aria-hidden />
      <span className="truncate">{label}</span>
    </span>
  )
}

/**
 * The "applies to new sessions" hint, shown after a successful import that
 * reached no live browser session while the view is running.
 */
export function CookieNewSessionHint({ show }: { show: boolean }) {
  if (!show) return null
  return (
    <span
      className="inline-flex items-center gap-1 shrink-0 text-[11px] text-muted"
      data-testid="web-preview-cookies-new-session-hint"
    >
      <Check size={11} className="shrink-0 text-accent" aria-hidden />
      {i18nT('components.webPreviewPanel.cookies_apply_to_new_sessions')}
    </span>
  )
}

/**
 * The import dialog: explanation, file picker (.json/.txt via FileReader), paste
 * textarea, Import/Cancel. Rendered ONCE by the panel as a fixed overlay so it
 * shows over either header. `open` is controlled by the panel; `onDone` reports
 * whether the successful import reached no live session (so the panel can show
 * the new-session hint). A failed request renders through `ErrorNotice` and
 * keeps the dialog open; client-side validation stays an inline hint.
 */
export function CookieDialog({
  cookies,
  viewRunning,
  open,
  onClose,
  onImported,
}: {
  cookies: Cookies
  viewRunning: boolean
  open: boolean
  onClose: () => void
  onImported: (reachedNewSessionsOnly: boolean) => void
}) {
  const [paste, setPaste] = useState('')
  const [filename, setFilename] = useState<string | undefined>(undefined)
  // Two distinct things can go wrong here, and errors-use-error-notice draws the
  // line by where the VALUE comes from: `validationHint` is client-side ("paste
  // something first", "couldn't read that file") and is NOT an error surface;
  // `requestError` is the outcome of a request that FAILED (a 400 from the
  // parser, a 5xx) and renders through ErrorNotice.
  const [validationHint, setValidationHint] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const dialogTitleId = useId()

  const reset = useCallback(() => {
    setPaste('')
    setFilename(undefined)
    setValidationHint(null)
    setRequestError(null)
    if (fileRef.current) fileRef.current.value = ''
  }, [])

  const close = useCallback(() => { reset(); onClose() }, [reset, onClose])

  // Esc closes the dialog. A document listener rather than a handler on the
  // overlay keeps the backdrop free of keyboard handlers (a11y) and works
  // regardless of where focus sits inside the card.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, close])

  const onFile = useCallback((file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      setPaste(typeof reader.result === 'string' ? reader.result : '')
      setFilename(file.name)
      setValidationHint(null)
      setRequestError(null)
    }
    reader.onerror = () => setValidationHint(i18nT('components.webPreviewPanel.cookies_file_read_failed'))
    reader.readAsText(file)
  }, [])

  const submit = useCallback(async () => {
    const content = paste.trim()
    if (!content) {
      setValidationHint(i18nT('components.webPreviewPanel.cookies_paste_or_choose_a_file'))
      return
    }
    setValidationHint(null)
    setRequestError(null)
    try {
      const result = await cookies.importCookies(content, filename)
      const reachedNone = result.hot_load.loaded.length === 0
      onImported(viewRunning && reachedNone)
      close()
    } catch (e) {
      // A failed request (a 400 for malformed input, or anything else) is shown
      // through ErrorNotice; the dialog stays open so the user can fix the
      // paste and retry.
      setRequestError(requestErrorText(e, i18nT('components.webPreviewPanel.cookies_import_failed_generic')))
    }
  }, [paste, filename, cookies, viewRunning, onImported, close])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
      data-testid="web-preview-cookies-dialog"
    >
      {/* A real button behind the card carries the backdrop-dismiss, so the
          gesture is keyboard-reachable without hanging handlers on a div. */}
      <button
        type="button"
        aria-label={i18nT('components.webPreviewPanel.cookies_cancel')}
        tabIndex={-1}
        className="absolute inset-0 w-full h-full bg-transparent border-none cursor-default"
        onClick={close}
      />
      <div className="relative w-full max-w-[460px] rounded-lg border border-border bg-bg shadow-lg overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
          <Cookie size={15} className="shrink-0 text-accent" aria-hidden />
          <span id={dialogTitleId} className="text-[13px] font-medium text-text">
            {i18nT('components.webPreviewPanel.import_cookies')}
          </span>
          <div className="flex-1" />
          <Btn aria-label={i18nT('app.dismiss')} onClick={close} className="shrink-0">
            <X className="lucide-inline" />
          </Btn>
        </div>

        <div className="px-4 py-3 flex flex-col gap-3">
          <p className="text-[12px] text-muted leading-snug m-0">
            {i18nT('components.webPreviewPanel.cookies_dialog_explanation')}
          </p>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <Upload size={13} /> {i18nT('components.webPreviewPanel.cookies_choose_a_file')}
            </button>
            {filename && (
              <span className="text-[11px] text-muted truncate min-w-0" title={filename}>{filename}</span>
            )}
            <input
              ref={fileRef}
              type="file"
              accept=".json,.txt,application/json,text/plain"
              className="sr-only"
              aria-label={i18nT('components.webPreviewPanel.cookies_choose_a_file')}
              onChange={(e) => {
                const file = e.target.files?.[0]
                e.target.value = ''
                if (file) onFile(file)
              }}
            />
          </div>

          <textarea
            value={paste}
            onChange={(e) => { setPaste(e.target.value); setFilename(undefined); setValidationHint(null); setRequestError(null) }}
            placeholder={i18nT('components.webPreviewPanel.cookies_paste_placeholder')}
            aria-label={i18nT('components.webPreviewPanel.cookies_paste_label')}
            spellCheck={false}
            rows={6}
            className="w-full resize-y rounded-md border border-border bg-bg-elevated text-[12px] font-mono text-text placeholder:text-muted px-2 py-1.5 focus:border-accent outline-none"
          />

          {/* Client-side validation only ("paste something first", "couldn't
              read that file"): nothing has failed on the server, so this is a
              hint beside the field, not an error surface. */}
          {validationHint && (
            <div
              className="flex items-start gap-1.5 text-[11px] leading-snug text-muted"
              data-testid="web-preview-cookies-hint"
            >
              <AlertTriangle size={13} className="shrink-0 mt-0.5" aria-hidden />
              <span className="min-w-0">{validationHint}</span>
            </div>
          )}
          {/* No hand-off: this notice sits under the cookie-export textarea, whose
              paste is the unsaved draft — the failed import is exactly why it was
              NOT persisted. The hand-off navigates to the chat and unmounts this
              dialog, taking the paste with it; the remedy ("fix the export and
              retry") lives in the textarea the user is already in. */}
          <ErrorNotice
            message={requestError}
            title={i18nT('components.webPreviewPanel.cookies_import_failed_generic')}
            testId="web-preview-cookies-error"
            className="text-[12px]"
          />
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
          <button
            type="button"
            onClick={close}
            className="text-[12px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
          >
            {i18nT('components.webPreviewPanel.cookies_cancel')}
          </button>
          <button
            type="button"
            onClick={() => { void submit() }}
            disabled={cookies.importing}
            className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md bg-accent text-white hover:opacity-90 transition-opacity cursor-pointer border-none disabled:opacity-60 disabled:cursor-default"
            data-testid="web-preview-cookies-submit"
          >
            {cookies.importing ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
            {i18nT('components.webPreviewPanel.cookies_import_action')}
          </button>
        </div>
      </div>
    </div>
  )
}
