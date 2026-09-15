import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { i18nT } from '../../../i18n/t'
import { SHARE_REPO_URL } from './shareSupport'
import pose2 from '../../../assets/ghost-poses/pose-2.svg'
import pose5 from '../../../assets/ghost-poses/pose-5.svg'

/**
 * The share card itself — the DOM node html-to-image snapshots into the PNG.
 *
 * Deliberately styled with FIXED inline colors rather than theme variables:
 * the card is an outward-facing artifact posted to social feeds, so it must
 * look identical (and on-brand) whatever dashboard theme the author runs.
 * This is the one surface where ignoring the theming contract is the point.
 * All glyphs are lucide SVGs, never emoji: an emoji depends on the host OS
 * font and rasterizes as tofu on hosts without one, permanently, in the
 * image the user posts.
 */

export const CARD_W = 520

/** The product logo — the purple tile with the white ghost the dashboard
 *  header shows, served by the gateway at /logo.png. An <img> to a
 *  same-origin URL: html-to-image inlines it into the exported PNG. */
function AppIconMark() {
  return (
    <img
      src="/logo.png"
      alt=""
      aria-hidden="true"
      draggable={false}
      style={{ width: 44, height: 44, borderRadius: 12, display: 'block' }}
    />
  )
}

/** Mascot ghosts peeking over the card edges, as on the welcome screen.
 *  Decorative white-body poses from the design system, partially clipped by
 *  the card's rounded frame; positioned clear of the text columns so they
 *  never sit behind a caption. */
function PeekingGhosts() {
  return (
    <>
      <img
        src={pose2}
        alt=""
        aria-hidden="true"
        draggable={false}
        style={{ position: 'absolute', top: -16, right: 34, width: 58, transform: 'rotate(8deg)', pointerEvents: 'none' }}
      />
      <img
        src={pose5}
        alt=""
        aria-hidden="true"
        draggable={false}
        style={{ position: 'absolute', bottom: -20, left: 232, width: 52, transform: 'rotate(-10deg)', pointerEvents: 'none' }}
      />
    </>
  )
}

interface EditableBlockProps {
  text: string
  onEdit?: (text: string) => void
  style: React.CSSProperties
}

/** Both editable regions share this focus treatment: inline styles cannot
 *  express :focus-visible, so focus state drives a ring. The ring only exists
 *  while the node holds focus, and export happens after a button click has
 *  already blurred it — so it never leaks into the PNG. */
function EditableBlock({ text, onEdit, style }: EditableBlockProps) {
  const [focused, setFocused] = useState(false)
  return (
    <div
      role="textbox"
      aria-multiline="true"
      aria-label={i18nT('pages.chat.share.edit_hint')}
      title={i18nT('pages.chat.share.edit_hint')}
      contentEditable
      suppressContentEditableWarning
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onInput={(e) => onEdit?.((e.target as HTMLElement).innerText ?? '')}
      style={{
        ...style,
        outline: 'none',
        boxShadow: focused ? '0 0 0 2px rgba(255,255,255,0.85)' : undefined,
        cursor: 'text',
      }}
    >
      {text}
    </div>
  )
}

export interface ShareCardProps {
  /** The user question paired above the reply; omitted when not included. */
  question?: string
  /** Initial reply excerpt. Both text blocks are contentEditable — later
   *  edits live in the DOM (which the PNG snapshot reads) and are mirrored
   *  via the onEdit callbacks for the sensitive-content scan; React never
   *  re-renders the text back. */
  excerpt: string
  onExcerptEdit?: (text: string) => void
  onQuestionEdit?: (text: string) => void
}

export default function ShareCard({ question, excerpt, onExcerptEdit, onQuestionEdit }: ShareCardProps) {
  return (
    <div
      data-testid="share-card"
      data-share-card-root
      style={{
        width: CARD_W,
        boxSizing: 'border-box',
        padding: '26px 28px 22px',
        borderRadius: 20,
        // Solid brand purple, never a gradient: the brand system is flat, and a
        // flat fill also survives every platform's re-encode without banding.
        background: '#7c3aed',
        color: '#f5f3ff',
        fontFamily: "-apple-system, 'Segoe UI', 'PingFang SC', 'Noto Sans CJK SC', sans-serif",
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <PeekingGhosts />
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <AppIconMark />
        {/* Wordmark via catalog so a rebranded edition overrides one variable. */}
        <div style={{ fontSize: 17, fontWeight: 700, lineHeight: '22px' }}>{i18nT('pages.chat.share.card_wordmark')}</div>
      </div>

      {question && (
        <EditableBlock
          text={question}
          onEdit={onQuestionEdit}
          style={{
            marginTop: 18,
            padding: '10px 14px',
            borderRadius: 12,
            background: 'rgba(255,255,255,0.08)',
            fontSize: 13,
            lineHeight: '19px',
            color: '#e4defc',
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
          }}
        />
      )}

      <EditableBlock
        text={excerpt}
        onEdit={onExcerptEdit}
        style={{
          marginTop: question ? 10 : 18,
          padding: '14px 16px',
          borderRadius: 12,
          background: 'rgba(255,255,255,0.12)',
          fontSize: 14,
          lineHeight: '21px',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
        }}
      />

      <div
        style={{
          marginTop: 18,
          paddingTop: 12,
          borderTop: '1px solid rgba(255,255,255,0.22)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 700, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <Sparkles size={13} color="#f5f3ff" aria-hidden="true" />
          {i18nT('pages.chat.share.card_footer')}
        </span>
        <span style={{ fontSize: 11.5, color: '#cfc6ff' }}>{SHARE_REPO_URL.replace('https://', '')}</span>
      </div>
    </div>
  )
}
