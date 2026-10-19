/**
 * Mochi - One chat message bubble
 *
 * `Bubble` renders one transcript message: markdown and widgets, the tool
 * approval card with its scoped-trust rows, the option, edit and copy controls,
 * and the relative timestamp. The approval payload parser and the
 * external-approval check live here too. One of the owners `ChatPanel.tsx`
 * composes: the panel renders `Bubble`, and `./ChatPanel` re-exports `Bubble`,
 * `parseApproval` and `externalApprovalApproved`. Upstream Mochi keeps this code
 * in `src/renderer/ChatPanel.tsx`.
 */
import React from 'react'
import {
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Handshake,
  Shield,
  ShieldCheck,
  ShieldPlus,
  SquarePen,
  Wrench,
} from 'lucide-react'
import { familyGrantIsDistinct, trustBasePattern, truncateCommandLabel } from '../shared/trustPatterns'
import Markdown from 'react-markdown'
import { capWhitespaceRuns } from '../../../../utils/markdownDepthBound'
import { mdImageDestToPath } from '../../../../utils/fileTokens'
import { copyToClipboard } from '../../../../utils/clipboard'
import type { ChatMessage } from '../shared/types'
import { WidgetFrame, parseWidgets, hasWidgets } from './WidgetFrame'
import type { ApprovalTarget } from '../../panel/panelBridge'
import { i18nT } from '../../../../i18n/t'
import { useLanguageGeneration } from '../../../../i18n/useLanguageGeneration'
import { i18next } from '../../../../i18n'
import { LocalImage, MD_REHYPE, MD_REMARK, mdComponents } from './ChatPanelMarkdown'

/** Sentinel standing in for the tool name while the sentence is translated.
 *  U+0000 cannot occur in a catalog value, so the split is unambiguous. */
const TOOL_SLOT = '\u0000'

/**
 * Render an already-translated sentence with the tool name as a styled `<code>`.
 *
 * The sentence used to be two keys with the tool wedged between them
 * ("Wants to run" + tool + "— OK?"), which forces every language into English
 * word order and gives a translator two fragments neither of which is a
 * sentence. It is now ONE key with a `{{tool}}` placeholder the translation may
 * put anywhere; interpolating a sentinel and splitting on it recovers the
 * `<code>` styling without handing the layout back to the code.
 *
 * The caller passes the RESOLVED string, not the key: `check-i18n-keys.mjs`
 * resolves file-scope literals only, so a key arriving as a parameter is a key
 * the gate cannot verify — which would exempt this whole file from every i18n
 * check. Same reason `i18nKeys.ts` indexes its maps inside the `i18nT(...)` call.
 */
function renderAroundTool(sentence: string, tool: string): React.ReactNode {
  const [before, after = ''] = sentence.split(TOOL_SLOT)
  return (
    <>
      {before}
      <code style={{ background: 'var(--bg-input)', borderRadius: 3, padding: '1px 4px', fontSize: 11 }}>{tool}</code>
      {after}
    </>
  )
}

/** The payload the approval route writes after the `__approval__` marker. */
interface ApprovalPayload {
  id: string
  tool: string
  toolInput?: string
  /** Normalized command, for a grant scoped to exactly this command. */
  fullCommand?: string
  /** Comma-joined base binaries ("cat,wc"), for a grant scoped to the family. */
  baseCommand?: string
  /** Server proof that this pending card may create a durable trust grant. */
  trustGrantable?: boolean
  /** The owner-bound target of this card's own request (see panelBridge). */
  target?: ApprovalTarget
}

/** Parse an approval payload, or null when the text is not really one of ours.
 *
 *  Shape-checked, not just JSON-checked: `__approval__"hi"` parses to a string, and
 *  reading `.tool` off it would render `undefined` into the bubble instead of the
 *  message the user typed.
 */
export function parseApproval(raw: string): ApprovalPayload | null {
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { id, tool } = parsed as Record<string, unknown>
    if (typeof id !== 'string' || typeof tool !== 'string') return null
    return parsed as ApprovalPayload
  } catch {
    return null
  }
}

/**
 * Whether an EXTERNAL approval resolution (dashboard / Slack / another surface)
 * was an approve. Anything that is not an explicit `approved: true` — a reject,
 * a missing flag, a malformed frame — must NOT read as approved: rendering a
 * rejected tool as "Approved" is a fabricated security decision.
 */
export function externalApprovalApproved(data?: Record<string, unknown>): boolean {
  return (data as { approved?: unknown } | undefined)?.approved === true
}

/** Shared look for the scoped-trust rows (full-width, quieter than the verbs). */
const trustScopeBtnStyle: React.CSSProperties = {
  background: 'var(--bg-input)',
  color: 'var(--text)',
  // Longhand: the shorthand value ('1px solid rgba(...)') reads as prose to the
  // i18n lint, which scans module constants. Same border, no word to mistake for copy.
  borderWidth: 1,
  borderStyle: 'solid',
  borderColor: 'rgba(255,180,0,0.25)',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  borderRadius: 6,
  padding: '3px 8px',
  fontSize: 11,
  cursor: 'pointer',
  textAlign: 'left',
}

// Exported for the capture harness (capture/mochi-trust-label.tsx), which mounts
// the real approval card as screenshot evidence; not part of the app's API.
export const Bubble = React.memo<{ message: ChatMessage; onOption?: (text: string) => void; onImageClick?: (b64: string) => void; onApproval?: (id: string, action: string, pattern?: string, trustGrantable?: boolean, target?: ApprovalTarget) => void; onEdit?: (content: string) => void; animate?: boolean }>(({ message, onOption, onImageClick, onApproval, onEdit, animate = true }) => {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const mounted = React.useRef(false)
  const shouldAnimate = animate && !mounted.current
  const [copied, setCopied] = React.useState(false)
  // Whether the scoped-trust grants are revealed. Declared here with the other
  // Bubble state: this component early-returns for several message shapes, so a
  // hook added lower down would run conditionally.
  const [trustOpen, setTrustOpen] = React.useState(false)
  React.useEffect(() => { mounted.current = true }, [])

  const content = message.content.replace(/^\n+/, '')

  // Approval message.
  //
  // The prefix is an internal marker the approval route writes, but NOTHING stops a
  // user typing it: the composer echoes the message locally, so `__approval__x`
  // reached this parse and the uncaught SyntaxError took the whole panel down to the
  // error boundary. A payload that does not parse is treated as what it actually is
  // — ordinary text the user wrote.
  const approvalReq = content.startsWith('__approval__') ? parseApproval(content.slice(12)) : null
  if (approvalReq !== null) {
    const req = approvalReq
    // A family grant is only offered when it differs from the exact-command
    // grant — see src/shared/trustPatterns, which must stay equivalent to the
    // dashboard's TrustDropdown so both surfaces offer the same scopes.
    const showTrustBase = familyGrantIsDistinct(req.fullCommand, req.baseCommand)
    const hasTrustScopes = req.trustGrantable === true
      && (Boolean(req.fullCommand) || showTrustBase)
    const approvalActions = [
      ['approve', i18nT('apps.mochi.approval.btn_approve'), '#2e7d32', Check],
      ...(req.trustGrantable === true
        ? [[
          'trust',
          // With scopes this button only OPENS the tier list, so the bare verb is
          // right. Without them the same click IS the broadest grant, so the
          // button must name what it grants: consent has to match the scope, and
          // an unqualified "Trust" beside one tool reads as trusting that tool.
          hasTrustScopes
            ? i18nT('apps.mochi.approval.btn_trust')
            : i18nT('apps.mochi.approval.trust_all_tools'),
          '#1565c0',
          Handshake,
        ]]
        : []),
      ['reject', i18nT('apps.mochi.approval.btn_reject'), '#c62828', Ban],
    ] as [string, string, string, React.ComponentType<{ size?: number }>][]
    return (
      <div style={{ alignSelf: 'flex-start', maxWidth: '85%', animation: shouldAnimate ? 'msgIn 0.25s ease-out' : 'none' }}>
        <div style={{
          padding: '8px 10px', borderRadius: '10px 10px 10px 2px',
          background: 'var(--bubble-assistant)', border: '1px solid rgba(255,180,0,0.3)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, marginBottom: 4 }}><Wrench size={13} /> <span>{renderAroundTool(i18nT('apps.mochi.approval.inline_ask', { tool: TOOL_SLOT }), req.tool)}</span></div>
          {req.toolInput && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, whiteSpace: 'pre-wrap', fontFamily: 'monospace', background: 'var(--bg-input)', borderRadius: 4, padding: 4 }}>{req.toolInput}</div>}
          <div style={{ display: 'flex', gap: 6 }}>
            {/* The icon is a component, not a glyph inside the translated
                string: an emoji in the copy can't follow the theme, renders at
                whatever size the font picks, and has to be repeated in every
                locale (AGENTS.md: emoji-as-icon is a bug). */}
            {approvalActions.map(
              ([action, label, bg, Icon]) => (
              <button
                key={action}
                // Trust is a SCOPE choice, not one verb. Tapping it reveals the
                // grants inline (see below) instead of firing the broadest one.
                // The action exists only when the gateway supplied grant proof;
                // a proven but scopeless card remains a direct broad action.
                onClick={() => {
                  if (action === 'trust' && hasTrustScopes) { setTrustOpen(v => !v); return }
                  if (req.target) onApproval?.(req.id, action, undefined, req.trustGrantable === true, req.target)
                  else onApproval?.(req.id, action, undefined, req.trustGrantable === true)
                }}
                aria-expanded={action === 'trust' && hasTrustScopes ? trustOpen : undefined}
                style={{
                background: bg, color: '#fff', border: 'none', display: 'inline-flex',
                alignItems: 'center', gap: 4,
                borderRadius: 6, padding: '3px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer',
              }}
              ><Icon size={12} />{label}{action === 'trust' && hasTrustScopes ? (trustOpen ? <ChevronDown size={10} /> : <ChevronRight size={10} />) : null}</button>
            ))}
          </div>
          {/* Scoped trust grants, narrowest first — mirroring the dashboard's
              TrustDropdown so the pet is not stuck with only the widest grant.
              Rendered inline rather than in a portaled menu: this panel is a
              ~380px frameless always-on-top window, where a portaled dropdown
              clips at the window edge. */}
          {trustOpen && hasTrustScopes && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 6 }}>
              {req.fullCommand && (
                <button
                  onClick={() => onApproval?.(req.id, 'trust_command', req.fullCommand, true)}
                  // The untruncated command as a tooltip: the label is budget-clamped,
                  // and this grant is an exact-string match, so the user must be able
                  // to read the whole thing before agreeing to it.
                  title={req.fullCommand}
                  style={trustScopeBtnStyle}
                ><Shield size={11} style={{ flexShrink: 0 }} />
                  {/* The label WRAPS rather than ellipsizing: the panel column
                      (~240px of text) shows only ~28 chars per line, so a CSS
                      ellipsis would re-collide the very labels the 64-char budget
                      distinguishes. minWidth:0 lets the flex item shrink;
                      overflowWrap:'anywhere' lets an unbreakable run (a sha, a
                      base64 arg) wrap instead of clipping past the panel edge.
                      whiteSpace:'pre-wrap' because the default COLLAPSES runs of
                      whitespace, which for an exact-string grant is an elision
                      one character wide: `grep "a  b" f` would render as
                      `grep "a b" f` while granting the two-space string. The
                      budget clamp above is a layout decision for this narrow
                      column; collapsing whitespace earns nothing anywhere. */}
                  {/* The 256-char clamp on this label is deliberate, and it is
                      where the pet differs from the dashboard, which renders the
                      exact-command label whole. This column is a narrow
                      frameless window whose width the app sets, so a multi-KB
                      command would wrap to over a hundred lines with no way to
                      widen it. This is a pointer surface and the button's
                      title carries the whole command, so hovering reads the
                      middle the clamp elides. */}
                  <span style={{ minWidth: 0, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
                    {i18nT('apps.mochi.approval.trust_this_command', { cmd: truncateCommandLabel(req.fullCommand) })}
                  </span></button>
              )}
              {showTrustBase && (
                <button
                  onClick={() => onApproval?.(req.id, 'trust_base', trustBasePattern(req.baseCommand ?? ''), true)}
                  style={trustScopeBtnStyle}
                ><ShieldPlus size={11} />{i18nT('apps.mochi.approval.trust_all_base', { base: (req.baseCommand ?? '').split(',').join(', ') })}</button>
              )}
              <button
                onClick={() => onApproval?.(req.id, 'trust', undefined, true)}
                style={trustScopeBtnStyle}
              ><ShieldCheck size={11} />{i18nT('apps.mochi.approval.trust_all_tools')}</button>
            </div>
          )}
          {req.trustGrantable === true && !hasTrustScopes && (
            // This hint renders ONLY on the scopeless path, where the button
            // grants the whole session. It therefore describes the session and
            // names no tool: naming the pending tool understates the grant.
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 5 }}>
              {i18nT('apps.mochi.approval.trust_hint')}
            </div>
          )}
        </div>
      </div>
    )
  }

  const optMatch = content.match(/\[options:\s*(.+?)\]\s*$/i)
  const text = optMatch ? content.slice(0, optMatch.index).trimEnd() : content
  const options = optMatch ? optMatch[1].split('|').map(o => o.trim()).filter(Boolean) : []
  const isUser = message.role === 'user'

  // Nothing to draw -> draw nothing.
  //
  // A turn whose text is only markup (a lone `<br>`, or whitespace) used to
  // produce a bubble with an empty body and a timestamp. It was masked while raw
  // HTML rendered as literal text -- you saw "<br>" and read it as a glitch in a
  // real message -- and became a visibly blank bubble once the markdown pipeline
  // started honouring the tag. Attachments, options and widgets each count as
  // content, so only a genuinely empty message is dropped.
  const hasVisibleText = text.replace(/<[^>]*>/g, '').trim() !== ''
  // Bound to a const so the narrowing survives into the click handler below, which
  // is a closure and would otherwise see the optional field again.
  const shot = message.screenshot
  const hasOtherContent =
    options.length > 0 ||
    !!shot ||
    text.includes('![') ||
    hasWidgets(text)
  if (!hasVisibleText && !hasOtherContent) return null

  return (
    <div className="msg-bubble" style={{ alignSelf: isUser ? 'flex-end' : 'flex-start', maxWidth: '85%', width: isUser ? 'fit-content' : undefined, display: 'flex', flexDirection: 'column', alignItems: isUser ? 'flex-end' : 'flex-start', animation: shouldAnimate ? 'msgIn 0.25s ease-out' : 'none' }}>
      {/* Bubble with tail */}
      <div style={{ position: 'relative', display: 'inline-block', maxWidth: '100%' }}>
        <div style={{
          padding: '6px 10px',
          borderRadius: isUser ? '10px 10px 2px 10px' : '10px 10px 10px 2px',
          background: isUser ? 'var(--bubble-user)' : 'var(--bubble-assistant)',
        }}>
          {shot && (
            // A real <button>, not a clickable <img>: the bubble caps the image at
            // 200px, so opening it full size is a CONTROL and must be tab-reachable
            // and Enter/Space-activated. Zero chrome and the same width bounds as the
            // image had, so the thumbnail is unchanged; the button carries the
            // accessible name, hence the empty alt.
            <button type="button" onClick={() => onImageClick?.(shot)}
              aria-label={i18nT('apps.mochi.chatPanel.open_image')}
              style={{ display: 'block', width: '100%', maxWidth: 200, padding: 0, border: 'none', background: 'none', cursor: 'zoom-in' }}>
              <img src={`data:image/png;base64,${shot}`} alt=""
                style={{ width: '100%', borderRadius: 4, marginBottom: 4, cursor: 'zoom-in' }} />
            </button>
          )}
          <div style={{ wordBreak: 'break-word', fontSize: 12, lineHeight: 1.4, overflow: 'hidden' }}>
            {isUser
              ? <span style={{ whiteSpace: 'pre-wrap' }}>
                  {text.split(/\n/).map((line, i) => {
                    // Detect markdown image syntax: ![...](path). The path may
                    // be mdImageDest's `<…>`-wrapped form (attachmentLines in
                    // composerDrop routes uploads through it) — resolve it with
                    // the shared wrap-aware inverse; unwrapped legacy paths
                    // are preserved verbatim (issue #3497).
                    const mdImgMatch = line.match(/^!\[[^\]]*\]\((.+)\)$/)
                    if (mdImgMatch) {
                      return <LocalImage key={i} path={mdImageDestToPath(mdImgMatch[1])} onClickImage={onImageClick} />
                    }
                    // Detect bare image file paths — line starts with / and ends with image extension.
                    const trimmed = line.trim()
                    if (trimmed.startsWith('/') && /\.(?:png|jpg|jpeg|gif|webp)$/i.test(trimmed)) {
                      return <LocalImage key={i} path={trimmed} onClickImage={onImageClick} />
                    }
                    return <React.Fragment key={i}>{i > 0 && '\n'}{line}</React.Fragment>
                  })}
                </span>
              : (() => {
                  // Extract image paths from markdown ![](path) and bare /path.png references.
                  // Paths may contain spaces so match broadly: / ... .ext
                  const imgRe = /!\[[^\]]*\]\(`?(\/[^`\n]+\.(?:png|jpg|jpeg|gif|webp))`?\)/gi
                  const images: string[] = []
                  let cleanText = text.replace(imgRe, (_, p) => { images.push(p.trim()); return '' })
                  const bareRe = /`?(\/[^`\n]+\.(?:png|jpg|jpeg|gif|webp))`?/gi
                  cleanText = cleanText.replace(bareRe, (_, p) => { images.push(p.trim()); return '' }).trim()

                  // Render mcwidget blocks inline
                  if (hasWidgets(cleanText)) {
                    const segments = parseWidgets(cleanText)
                    return <>
                      {segments.map((seg, i) => seg.type === 'widget'
                        ? <WidgetFrame key={i} html={seg.content} title={seg.title} />
                        : <Markdown key={i} remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{capWhitespaceRuns(seg.content)}</Markdown>
                      )}
                      {images.map((p, i) => <LocalImage key={`img-${i}`} path={p} onClickImage={onImageClick} />)}
                    </>
                  }

                  return <>
                    {cleanText && <Markdown remarkPlugins={MD_REMARK} rehypePlugins={MD_REHYPE} components={mdComponents}>{capWhitespaceRuns(cleanText)}</Markdown>}
                    {images.map((p, i) => <LocalImage key={i} path={p} onClickImage={onImageClick} />)}
                  </>
                })()
            }
          </div>
          {options.length > 0 && onOption && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
              {options.map((opt) => (
                <button key={opt} onClick={() => onOption(opt)} style={{
                  background: 'var(--accent-glow)', border: '1px solid var(--border-focus)',
                  borderRadius: 6, padding: '3px 10px', color: 'var(--accent)', fontSize: 11,
                  cursor: 'pointer',
                }}>{opt}</button>
              ))}
            </div>
          )}
        </div>
      </div>
      {/* Timestamp + copy/edit */}
      {message.timestamp > 0 && (
        <div style={{
          fontSize: 10, color: 'var(--text-faint)', marginTop: 2,
          display: 'flex', alignItems: 'center', gap: 4,
          justifyContent: isUser ? 'flex-end' : 'flex-start',
          paddingLeft: isUser ? 0 : 4, paddingRight: isUser ? 4 : 0,
        }}>
          {isUser && onEdit && (
            <button
              className="copy-md-btn"
              onClick={() => onEdit(message.content)}
              title={i18nT('apps.mochi.chatPanel.edit_resend')}
              aria-label={i18nT('apps.mochi.chatPanel.edit_resend')}
              style={{
                background: 'none', border: 'none', padding: '2px 3px',
                cursor: 'pointer', color: 'var(--text-faint)',
                opacity: 0, transition: 'opacity 0.15s, transform 0.15s',
                lineHeight: 1, display: 'flex', alignItems: 'center',
                borderRadius: 3,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.transform = 'scale(1.2)'; e.currentTarget.style.color = 'var(--text-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)'; e.currentTarget.style.color = 'var(--text-faint)' }}
            ><SquarePen size={11} /></button>
          )}
          <span>{relativeTime(message.timestamp)}</span>
          {!isUser && (
            <button
              className="copy-md-btn"
              onClick={() => {
                copyToClipboard(text).then((ok) => {
                  if (!ok) return
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1500)
                })
              }}
              title={copied ? i18nT('apps.mochi.chatPanel.copied') : i18nT('apps.mochi.chatPanel.copy_markdown')}
              aria-label={copied ? i18nT('apps.mochi.chatPanel.copied') : i18nT('apps.mochi.chatPanel.copy_markdown')}
              style={{
                background: 'none', border: 'none', padding: '2px 3px',
                cursor: 'pointer', color: copied ? 'var(--accent)' : 'var(--text-faint)',
                opacity: copied ? 1 : 0, transition: 'opacity 0.15s, transform 0.15s, color 0.15s',
                lineHeight: 1, display: 'flex', alignItems: 'center',
                borderRadius: 3,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.transform = 'scale(1.2)'; e.currentTarget.style.color = 'var(--text-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)'; e.currentTarget.style.color = copied ? 'var(--accent)' : 'var(--text-faint)' }}
            >{copied
              ? <Check size={12} strokeWidth={2.5} />
              : <Copy size={12} />
            }</button>
          )}
        </div>
      )}
    </div>
  )
}, (prev, next) => prev.message.id === next.message.id && prev.message.content === next.message.content && prev.animate === next.animate)

function relativeTime(ts: number): string {
  // The active locale is already a BCP-47 tag, which is what Intl wants. The
  // language-NAME mapper this replaced existed only because the setting used to
  // hold 'English'/'Chinese' rather than a code.
  const locale = i18next.language || 'en'
  const diff = Math.floor((Date.now() - ts) / 1000)
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (Math.abs(diff) < 10) return rtf.format(0, 'second')
  if (Math.abs(diff) < 60) return rtf.format(-diff, 'second')
  const mins = Math.floor(diff / 60)
  if (mins < 60) return rtf.format(-mins, 'minute')
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return rtf.format(-hrs, 'hour')
  const days = Math.floor(hrs / 24)
  if (days < 7) return rtf.format(-days, 'day')
  return new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(ts)
}
