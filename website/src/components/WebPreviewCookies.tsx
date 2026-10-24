import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Cookie, Upload, Loader2, AlertTriangle, Check, Trash2, FileText } from 'lucide-react'

import { ApiError } from '../api/apiError'
import { fmtNumber, fmtRelative } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { previewCookieExport } from '../lib/cookieExportPreview'
import type { useBrowserCookies } from '../hooks/useBrowserCookies'
import { DropdownMenuItem } from './ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import Modal from './Modal'

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

/** How many domains the dialog's pre-import preview names before "+N more". */
const PREVIEW_DOMAIN_CAP = 3

/** The preview's domain list: the first few, then a localized "+N more". */
function previewDomains(domains: string[]): string {
  const shown = domains.slice(0, PREVIEW_DOMAIN_CAP).join(', ')
  const rest = domains.length - PREVIEW_DOMAIN_CAP
  if (rest <= 0) return shown
  return `${shown}, ${i18nT('components.webPreviewPanel.cookies_domains_more', { count: fmtNumber(rest) })}`
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
  const errorId = useId()
  if (cookies.forbidden) return null
  // A status read that kept failing (retries are bounded in the hook) is said
  // here, beside the control it affects, with the hand-off as its own focus stop
  // the way `CookieClearMenuItem` renders a failed clear.
  const failure = cookies.error
    ? requestErrorText(cookies.error, i18nT('components.webPreviewPanel.cookies_status_failed'))
    : null
  return (
    <>
      <DropdownMenuItem onSelect={onOpen} data-testid="web-preview-import-cookies">
        <Cookie size={13} className="shrink-0 text-muted" />
        <span>{i18nT('components.webPreviewPanel.import_cookies')}</span>
        {failure && (
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
              testId="web-preview-cookies-status-error"
            />
          </span>
        )}
      </DropdownMenuItem>
      {failure && (
        <ErrorNoticeMenuItem Item={DropdownMenuItem} message={failure} describedBy={errorId} />
      )}
    </>
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
 * A DELETE that succeeded on disk but whose `live.failed` is non-empty still
 * failed to clear some open browsers. That partial failure uses the same
 * passive alert and sibling hand-off, naming what remains and when it ends.
 * The row stays mounted (disabled — there is nothing further to clear) while
 * that notice is showing, so the outcome does not vanish with the chip.
 *
 * Hidden for a non-owner and when nothing is imported (and no notice is up).
 */
export function CookieClearMenuItem({ cookies }: { cookies: Cookies }) {
  const [armed, setArmed] = useState(false)
  const armTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const errorId = useId()
  const failure = cookies.clearError
    ? requestErrorText(cookies.clearError, i18nT('components.webPreviewPanel.cookies_clear_failed'))
    : null
  const stillHolding = Object.keys(cookies.lastClear?.live?.failed ?? {}).length
  const partial = stillHolding > 0
    ? i18nT('components.webPreviewPanel.cookies_clear_live_failed', { count: fmtNumber(stillHolding) })
    : null

  useEffect(() => () => { if (armTimer.current) clearTimeout(armTimer.current) }, [])

  const onSelect = useCallback((event: Event) => {
    // Keep the menu open: the armed state and the outcome both render here.
    event.preventDefault()
    if (cookies.clearing) return
    if (!armed) {
      setArmed(true)
      cookies.resetClear()
      armTimer.current = setTimeout(() => setArmed(false), CLEAR_ARM_MS)
      return
    }
    if (armTimer.current) clearTimeout(armTimer.current)
    setArmed(false)
    void cookies.clear().catch(() => {})
  }, [armed, cookies])

  const present = Boolean(cookies.data?.present)
  if (cookies.forbidden || (!present && !failure && !partial)) return null

  const label = armed
    ? i18nT('components.webPreviewPanel.cookies_clear_confirm')
    : i18nT('components.webPreviewPanel.cookies_clear')
  const clearError = failure ?? partial

  return (
    <>
      <DropdownMenuItem
        onSelect={onSelect}
        disabled={cookies.clearing || !present}
        data-testid="web-preview-cookies-clear"
        data-armed={armed || undefined}
      >
        {cookies.clearing
          ? <Loader2 size={13} className="shrink-0 animate-spin text-muted" />
          : <Trash2 size={13} className={`shrink-0 ${armed ? 'text-danger' : 'text-muted'}`} />}
        {/* The armed colour sits on the row's children, not the menu item: the
            item's own colour belongs to the primitive. */}
        <span className={`flex-1 ${armed ? 'text-danger' : ''}`}>{label}</span>
        {clearError && (
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
              message={clearError}
              variant="inline"
              testId="web-preview-cookies-clear-error"
            />
          </span>
        )}
      </DropdownMenuItem>
      {clearError && (
        <ErrorNoticeMenuItem
          Item={DropdownMenuItem}
          message={clearError}
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
 * The import dialog, on the shared `Modal`: a plain opening sentence (which
 * sites, for how long, how to undo), a short "How to export" list, the file
 * picker (.json/.txt via FileReader) and the paste textarea, then Import/Cancel.
 * Rendered ONCE by the panel. `Modal` gives it the keyboard isolation every
 * dialog needs: the page's global shortcuts never see a keystroke typed here, so
 * no chord can navigate away and unmount the dialog with the unsaved paste in
 * it. While the paste holds text the backdrop and Escape do not dismiss it, and
 * while the import is in flight nothing does.
 *
 * A failed operation (the request, or reading the chosen file) renders through
 * `ErrorNotice` and keeps the dialog open; only "nothing to import yet" is an
 * inline hint, because nothing has failed. `onImported` reports whether the
 * successful import reached no live session (so the panel can show the
 * new-session hint).
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
  // errors-use-error-notice draws the line by whether something FAILED:
  // `validationHint` is "paste something first", where nothing has, and is not
  // an error surface; `failure` is an operation that did fail (reading the
  // chosen file, or the import request) and renders through ErrorNotice.
  const [validationHint, setValidationHint] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  // The set was saved, but delivery to new or open browsers failed.
  const [liveNotice, setLiveNotice] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const preview = useMemo(() => previewCookieExport(paste), [paste])

  const reset = useCallback(() => {
    setPaste('')
    setFilename(undefined)
    setValidationHint(null)
    setFailure(null)
    setLiveNotice(null)
    setSaved(false)
    if (fileRef.current) fileRef.current.value = ''
  }, [])

  const close = useCallback(() => { reset(); onClose() }, [reset, onClose])

  const onFile = useCallback((file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      setPaste(typeof reader.result === 'string' ? reader.result : '')
      setFilename(file.name)
      setValidationHint(null)
      setFailure(null)
      setLiveNotice(null)
      setSaved(false)
    }
    reader.onerror = () => {
      setValidationHint(null)
      setFailure(i18nT('components.webPreviewPanel.cookies_file_read_failed'))
    }
    reader.readAsText(file)
  }, [])

  const submit = useCallback(async () => {
    if (saved) return
    // Trim only to test for blank input. The export is sent unchanged: a
    // Netscape line whose value is empty ends in a tab, and trimming it away
    // leaves six columns, so the parser would drop that cookie.
    if (!paste.trim()) {
      setValidationHint(i18nT('components.webPreviewPanel.cookies_paste_or_choose_a_file'))
      return
    }
    setValidationHint(null)
    setFailure(null)
    setLiveNotice(null)
    try {
      const result = await cookies.importCookies(paste)
      setSaved(true)
      // Saving is not delivery: an unreachable host must never promise that
      // a new browser session will receive the imported set.
      if (result.hot_load.deliverable === false) {
        onImported(false)
        setLiveNotice(i18nT('components.webPreviewPanel.cookies_import_unreachable'))
        return
      }
      // Saved, but some live sessions refused it: say so and keep the dialog
      // open, rather than closing as if every browser now carries the set.
      const refused = Object.keys(result.hot_load.failed ?? {}).length
      if (refused > 0) {
        onImported(false)
        setLiveNotice(i18nT('components.webPreviewPanel.cookies_import_live_failed', {
          count: fmtNumber(refused),
        }))
        return
      }
      const reachedNone = result.hot_load.loaded.length === 0
      onImported(viewRunning && reachedNone)
      close()
    } catch (e) {
      // A failed request (a 400 for malformed input, a host that cannot reach
      // the agent's browser, or anything else); the dialog stays open so the
      // user can fix the paste and retry.
      setFailure(requestErrorText(e, i18nT('components.webPreviewPanel.cookies_import_failed_generic')))
    }
  }, [paste, saved, cookies, viewRunning, onImported, close])

  const footer = (
    <>
      <button
        type="button"
        onClick={close}
        disabled={cookies.importing}
        className="text-[12px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent disabled:opacity-60 disabled:cursor-default"
      >
        {saved
          ? i18nT('components.webPreviewPanel.cookies_done')
          : i18nT('components.webPreviewPanel.cookies_cancel')}
      </button>
      {!saved && (
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
      )}
    </>
  )

  return (
    <Modal
      open={open}
      onClose={close}
      title={i18nT('components.webPreviewPanel.import_cookies')}
      maxWidth={480}
      footer={footer}
      guardAccidentalDismiss={paste.trim() !== ''}
      dismissDisabled={cookies.importing}
    >
      <div className="flex flex-col gap-3" data-testid="web-preview-cookies-dialog">
        <p className="text-[13px] text-text leading-snug m-0" data-testid="web-preview-cookies-intro">
          {i18nT('components.webPreviewPanel.cookies_dialog_intro')}
        </p>
        <p className="text-[12px] text-muted leading-snug m-0">
          {i18nT('components.webPreviewPanel.cookies_dialog_values_stay')}
        </p>

        <div className="flex flex-col gap-1">
          <span className="text-[12px] font-medium text-text">
            {i18nT('components.webPreviewPanel.cookies_how_to_export')}
          </span>
          <ul className="m-0 pl-4 list-disc text-[12px] text-muted leading-snug" data-testid="web-preview-cookies-how-to-export">
            <li>{i18nT('components.webPreviewPanel.cookies_export_extension')}</li>
            <li>{i18nT('components.webPreviewPanel.cookies_export_cookies_txt')}</li>
            <li>{i18nT('components.webPreviewPanel.cookies_export_playwright')}</li>
          </ul>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={cookies.importing}
            className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
          >
            <FileText size={13} aria-hidden /> {i18nT('components.webPreviewPanel.cookies_choose_a_file')}
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
            data-testid="web-preview-cookies-file"
            disabled={cookies.importing}
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = ''
              if (file) onFile(file)
            }}
          />
        </div>

        <textarea
          value={paste}
          disabled={cookies.importing}
          onChange={(e) => {
            setPaste(e.target.value)
            setFilename(undefined)
            setValidationHint(null)
            setFailure(null)
            setLiveNotice(null)
            setSaved(false)
          }}
          placeholder={i18nT('components.webPreviewPanel.cookies_paste_placeholder')}
          aria-label={i18nT('components.webPreviewPanel.cookies_paste_label')}
          spellCheck={false}
          rows={6}
          className="w-full resize-y rounded-md border border-border bg-bg-elevated text-[12px] font-mono text-text placeholder:text-muted px-2 py-1.5 focus:border-accent outline-none"
        />

        {/* Which sites Import would sign the agent in to, named BEFORE the
            user commits. Read in the browser and advisory only: the server
            parses the export again and is the one that refuses a bad one, so
            an export this reader cannot parse shows nothing here. */}
        {preview && (
          <p
            className="text-[12px] text-text leading-snug m-0 break-words"
            data-testid="web-preview-cookies-preview"
          >
            {i18nT('components.webPreviewPanel.cookies_preview', {
              domains: previewDomains(preview.domains),
              cookies: i18nT('components.webPreviewPanel.cookies_count', { count: fmtNumber(preview.cookieCount) }),
            })}
          </p>
        )}

        {/* "Paste something first": nothing has failed, so this is a hint
            beside the field, not an error surface. */}
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
            paste is the unsaved draft, and the failure is exactly why it was
            NOT persisted. The hand-off navigates to the chat and unmounts this
            dialog, taking the paste with it; the remedy ("fix the export, or
            choose the file again, and retry") lives in the dialog itself. */}
        <ErrorNotice
          message={failure}
          title={i18nT('components.webPreviewPanel.cookies_import_failed_generic')}
          testId="web-preview-cookies-error"
          className="text-[12px]"
        />
        {/* Hand-off is safe: the set is already saved, and editing the textarea
            or choosing a file clears this notice, so it only shows while the
            textarea holds exactly the export just saved. `onHandoff` closes the
            dialog so it does not sit over the chat the hand-off opens. */}
        <ErrorNotice
          message={liveNotice}
          testId="web-preview-cookies-live-error"
          className="text-[12px]"
          askAgent
          onHandoff={close}
        />
      </div>
    </Modal>
  )
}
