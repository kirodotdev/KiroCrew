import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Check, Copy, Download, Loader2 } from 'lucide-react'
import {
  Dialog, DialogContent, DialogHeader, DialogBody, DialogTitle, DialogDescription,
} from '../../../components/ui/dialog'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'
import { copyToClipboard } from '../../../utils/clipboard'
import ShareCard, { CARD_W } from './ShareCard'
import {
  SHARE_REPO_URL, X_POST_LIMIT, buildIntentUrl, clampExcerpt, copyImageWithText,
  downloadBlob, scanSensitive, type SensitiveKind,
} from './shareSupport'

/**
 * "Share to social media" dialog: renders the message as a branded card,
 * exports it as a PNG (download or clipboard), and opens X / LinkedIn intent
 * composers with the caption prefilled. Everything happens client-side — the
 * card never touches a server, which is what makes a pre-share sensitive-text
 * nudge sufficient rather than a hard gate.
 */

/** Literal keys per kind (a template key would evade the dead-key scanner). */
function kindLabel(kind: SensitiveKind): string {
  switch (kind) {
    case 'aws_key': return i18nT('pages.chat.share.kind_aws_key')
    case 'token': return i18nT('pages.chat.share.kind_token')
    case 'private_key': return i18nT('pages.chat.share.kind_private_key')
    case 'local_path': return i18nT('pages.chat.share.kind_local_path')
    case 'internal_url': return i18nT('pages.chat.share.kind_internal_url')
  }
}

export interface ShareMessageModalProps {
  onClose: () => void
  /** The assistant reply being shared (steer markers already stripped). */
  messageText: string
  /** The user question this reply answered, when the host can supply it. */
  prevUserText?: string
  /** The `capabilities.social_share` governance answer. When it flips to false
   *  while this dialog is open, the dialog stays mounted so the user's edits are
   *  not destroyed: the actions are withdrawn and a notice says why. */
  shareEnabled: boolean
}

export default function ShareMessageModal({ onClose, messageText, prevUserText, shareEnabled }: ShareMessageModalProps) {
  const initialExcerpt = useMemo(() => clampExcerpt(messageText), [messageText])
  // Q&A pairs travel best on social feeds, so the question defaults IN.
  const [includeQuestion, setIncludeQuestion] = useState(!!prevUserText)
  const [caption, setCaption] = useState(() => i18nT('pages.chat.share.caption_template', { link: SHARE_REPO_URL }))
  // Mirrors of the card's contentEditable text; feed the scan, never the DOM.
  const [excerpt, setExcerpt] = useState(initialExcerpt)
  const [questionEdit, setQuestionEdit] = useState<string | null>(null)
  const [busy, setBusy] = useState<'download' | 'copy' | 'intent' | null>(null)
  const [feedback, setFeedback] = useState<'copied' | 'copy_unavailable' | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  // Latest permission for the async handlers: a click captures the closure's
  // value at click time, but the answer can change while the export awaits.
  const shareEnabledRef = useRef(shareEnabled)
  shareEnabledRef.current = shareEnabled
  const [textCopy, setTextCopy] = useState<'idle' | 'copied' | 'failed'>('idle')
  const captionRef = useRef<HTMLTextAreaElement>(null)

  // The preview scales DOWN to fit narrow viewports (the card itself keeps its
  // fixed export width — the transform sits on a wrapper, which html-to-image
  // never serializes, so the PNG is always the full-size card). The outer
  // spacer takes the scaled height so no dead gap is left under the preview.
  // The node arrives via state (callback ref), not a ref object: the dialog
  // body mounts a commit after the component's first effect pass, and an
  // effect keyed on the node is what re-arms the observer when it appears.
  const [fitEl, setFitEl] = useState<HTMLDivElement | null>(null)
  const [fit, setFit] = useState({ scale: 1, height: 0 })
  useEffect(() => {
    if (!fitEl || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const scale = Math.min(1, fitEl.clientWidth / CARD_W)
      const card = fitEl.querySelector<HTMLElement>('[data-share-card-root]')
      setFit({ scale, height: card ? card.offsetHeight * scale : 0 })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(fitEl)
    return () => ro.disconnect()
  }, [fitEl])

  const initialQuestion = includeQuestion && prevUserText ? clampExcerpt(prevUserText, 180) : undefined
  const question = initialQuestion !== undefined && questionEdit !== null ? questionEdit : initialQuestion
  // The withdrawn state's one salvage path: a plain-text copy of EVERYTHING the
  // user could have edited here — the caption and the card's text (question,
  // excerpt), which live in a contentEditable and vanish on close just like the
  // caption does. Local only (clipboard, no image, no site), so it stays
  // available when sharing is not. The clipboard can refuse (plain-HTTP hosts
  // return false; the legacy fallback can throw): the notice points the user at
  // THIS button, so a refusal must not look like success or leave the button
  // inert — it says so, and selects the caption so a manual copy is one
  // keystroke away.
  const copyTextOnly = async () => {
    const salvage = [caption, question, excerpt].filter((s) => s && s.trim()).join('\n\n')
    let ok = false
    try { ok = await copyToClipboard(salvage) } catch { ok = false }
    if (!ok) { captionRef.current?.focus(); captionRef.current?.select() }
    setTextCopy(ok ? 'copied' : 'failed')
  }
  // Card and caption are scanned SEPARATELY so the warning can assert where
  // the match sits. A combined scan labelled "the card" misdirects the check:
  // a credential in the caption sends the user hunting through a clean card,
  // and the dismissed warning ships the secret.
  const cardFindings = useMemo(
    () => scanSensitive([question ?? '', excerpt].join('\n')),
    [question, excerpt],
  )
  const captionFindings = useMemo(() => scanSensitive(caption), [caption])

  /** Rasterize the live card DOM at 2x. html-to-image is loaded on demand so
   *  the chat bundle never pays for it before the first share. */
  const exportBlob = async (): Promise<Blob | null> => {
    const node = wrapRef.current?.querySelector<HTMLElement>('[data-share-card-root]')
    if (!node) return null
    const { toBlob } = await import('html-to-image')
    return toBlob(node, { pixelRatio: 2, cacheBust: true })
  }

  const handleDownload = async () => {
    setBusy('download'); setFeedback(null)
    try {
      const blob = await exportBlob()
      if (blob) downloadBlob(blob, `kiro-crew-share-${Date.now()}.png`)
    } finally { setBusy(null) }
  }

  const handleCopy = async () => {
    setBusy('copy'); setFeedback(null)
    try {
      const blob = await exportBlob()
      if (!blob) return
      if (await copyImageWithText(blob, caption)) {
        setFeedback('copied')
      } else {
        // No clipboard (Firefox, permissions): the image still reaches the
        // user as a download rather than the button silently doing nothing.
        downloadBlob(blob, `kiro-crew-share-${Date.now()}.png`)
        setFeedback('copy_unavailable')
      }
    } finally { setBusy(null) }
  }

  /** Intent composers accept TEXT only, so the card must already be on the
   *  clipboard when the composer opens — auto-copy (download on a clipboard
   *  refusal) before opening, so a first-time user clicking Share directly
   *  never publishes a caption-only post. The tab is opened synchronously in
   *  the click's own call stack (popup blockers judge the open, not the later
   *  navigation), its opener severed, and it is pointed at the composer once
   *  the export has settled. A blocker that still refuses leaves the copy
   *  done and the composer one click away — never data. */
  const openIntent = async (platform: 'x' | 'linkedin') => {
    const url = buildIntentUrl(platform, caption)
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    setBusy('intent'); setFeedback(null)
    try {
      const blob = await exportBlob()
      if (blob) {
        if (await copyImageWithText(blob, caption)) {
          setFeedback('copied')
        } else {
          downloadBlob(blob, `kiro-crew-share-${Date.now()}.png`)
          setFeedback('copy_unavailable')
        }
      }
    } finally { setBusy(null) }
    // The permission is re-read AFTER the awaits, from the ref rather than the
    // closure: a policy swap during the export must not be followed by the
    // navigation that hands the caption to the third-party site. The tab that
    // was pre-opened to dodge the popup blocker is closed instead of left blank.
    if (!shareEnabledRef.current) {
      tab?.close()
      return
    }
    if (tab) tab.location.href = url
    else window.open(url, '_blank', 'noopener,noreferrer')
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent maxWidth={960}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.chat.share.title')}</DialogTitle>
          <DialogDescription>{i18nT('pages.chat.share.description')}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col lg:flex-row gap-5">
            {/* Card preview (the export source) */}
            <div ref={wrapRef} className="min-w-0 w-full lg:w-[520px] lg:shrink-0 self-center lg:self-start">
              <div className="text-[11px] leading-4 text-muted mb-1.5 tracking-wide">{i18nT('pages.chat.share.preview_label')} · {i18nT('pages.chat.share.edit_hint')}</div>
              <div ref={setFitEl} style={fit.height ? { height: fit.height } : undefined}>
                <div style={{ transform: `scale(${fit.scale})`, transformOrigin: 'top left', width: CARD_W }}>
                  <ShareCard question={initialQuestion} excerpt={initialExcerpt} onExcerptEdit={setExcerpt} onQuestionEdit={setQuestionEdit} />
                </div>
              </div>
            </div>

            {/* Controls */}
            <div className="flex flex-col gap-3 min-w-0 flex-1">
              {prevUserText && (
                <label className="flex items-center gap-2 text-[13px] leading-5 text-text cursor-pointer select-none">
                  <input type="checkbox" aria-label={i18nT('pages.chat.share.include_question')} checked={includeQuestion} onChange={(e) => { setIncludeQuestion(e.target.checked); setQuestionEdit(null) }} />
                  {i18nT('pages.chat.share.include_question')}
                </label>
              )}

              <div>
                <label htmlFor="share-caption" className="block text-[11px] leading-4 text-muted mb-1.5 tracking-wide">{i18nT('pages.chat.share.caption_label')}</label>
                <textarea
                  id="share-caption"
                  ref={captionRef}
                  aria-label={i18nT('pages.chat.share.caption_label')}
                  className="w-full h-28 rounded-lg bg-bg ring-1 ring-inset ring-border focus:ring-accent outline-none px-3 py-2 text-[13px] leading-5 text-text resize-none"
                  value={caption}
                  onChange={(e) => setCaption(e.target.value)}
                />
                <div className={`text-right text-[11px] leading-4 tabular-nums ${caption.length > X_POST_LIMIT ? 'text-danger' : 'text-muted'}`}>
                  {i18nT('pages.chat.share.char_count', { count: fmtNumber(caption.length), limit: fmtNumber(X_POST_LIMIT) })}
                </div>
              </div>

              {(cardFindings.length > 0 || captionFindings.length > 0) && (
                <div role="alert" className="flex items-start gap-2 rounded-lg bg-warn-subtle ring-1 ring-inset ring-warn/30 px-3 py-2 text-[12px] leading-5 text-text">
                  <AlertTriangle size={14} className="shrink-0 mt-0.5 text-warn" aria-hidden="true" />
                  <span>
                    {cardFindings.length > 0 && <span className="block">{i18nT('pages.chat.share.sensitive_in_card', { kinds: cardFindings.map(kindLabel).join(', ') })}</span>}
                    {captionFindings.length > 0 && <span className="block">{i18nT('pages.chat.share.sensitive_in_caption', { kinds: captionFindings.map(kindLabel).join(', ') })}</span>}
                  </span>
                </div>
              )}

              {/* A policy swap while composing: say what happened instead of
                  making the dialog vanish with the user's edits in it. The
                  share actions below are withdrawn; the caption text keeps one
                  local salvage path so Close never means silent loss. */}
              {!shareEnabled && (
                <div>
                  <div role="alert" data-testid="share-withdrawn" className="flex items-start gap-2 rounded-lg bg-warn-subtle ring-1 ring-inset ring-warn/30 px-3 py-2 text-[12px] leading-5 text-text">
                    <AlertTriangle size={14} className="shrink-0 mt-0.5 text-warn" aria-hidden="true" />
                    <span className="flex-1">{i18nT('pages.chat.share.withdrawn_by_policy')}</span>
                    <Btn onClick={copyTextOnly} data-testid="share-copy-text" aria-label={i18nT('pages.chat.share.copy_text_only')}>
                      {textCopy === 'copied' ? <Check size={14} className="lucide-inline text-ok" /> : textCopy === 'failed' ? <AlertTriangle size={14} className="lucide-inline text-warn" /> : <Copy size={14} className="lucide-inline" />}
                      {' '}
                      {textCopy === 'copied' ? i18nT('pages.chat.share.copied_text') : textCopy === 'failed' ? i18nT('pages.chat.share.copy_text_failed') : i18nT('pages.chat.share.copy_text_only')}
                    </Btn>
                  </div>
                  {textCopy === 'failed' && (
                    <p role="status" data-testid="share-copy-text-unavailable" className="text-[12px] leading-5 text-muted m-0 mt-1.5">{i18nT('pages.chat.share.copy_text_unavailable')}</p>
                  )}
                </div>
              )}

              {/* Two actions per row (dialog action-row convention). */}
              <div className="grid grid-cols-2 gap-2">
                <Btn primary disabled={busy !== null || !shareEnabled} onClick={handleDownload} data-testid="share-download">
                  {busy === 'download' ? <Loader2 size={14} className="animate-spin lucide-inline" /> : <Download size={14} className="lucide-inline" />} {i18nT('pages.chat.share.download_png')}
                </Btn>
                <Btn disabled={busy !== null || !shareEnabled} onClick={handleCopy} data-testid="share-copy">
                  {busy === 'copy' ? <Loader2 size={14} className="animate-spin lucide-inline" /> : feedback === 'copied' ? <Check size={14} className="lucide-inline text-ok" /> : <Copy size={14} className="lucide-inline" />} {i18nT('pages.chat.share.copy_image_text')}
                </Btn>
                <Btn disabled={busy !== null || !shareEnabled} onClick={() => openIntent('x')} data-testid="share-x">{i18nT('pages.chat.share.share_on_x')}</Btn>
                <Btn disabled={busy !== null || !shareEnabled} onClick={() => openIntent('linkedin')} data-testid="share-linkedin">{i18nT('pages.chat.share.share_on_linkedin')}</Btn>
              </div>

              {shareEnabled && (
              <p className="text-[12px] leading-5 text-muted m-0" role={feedback ? 'status' : undefined}>
                {feedback === 'copied' ? i18nT('pages.chat.share.copied')
                  : feedback === 'copy_unavailable' ? i18nT('pages.chat.share.copy_unavailable')
                  : i18nT('pages.chat.share.intent_hint')}
              </p>
              )}
            </div>
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}
