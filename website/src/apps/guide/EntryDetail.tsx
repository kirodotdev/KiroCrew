// The detail pane for one guide entry: header + trust/community badge, symptom,
// steps, "if you're still stuck", the crew prompt (Copy + Send to chat), and
// sources. All prose runs through MdInline; language-aware via `pick`.
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { LifeBuoy, Wrench, Users, MessageSquarePlus, Link2 } from 'lucide-react'
import MdInline from './MdInline'
import TrustBadge from './TrustBadge'
import CopyButton from './CopyButton'
import StepBlock from './StepBlock'
import { type EntryDetail as Entry, pickL } from './api'

export default function EntryDetail({
  entry,
  ids,
  onSelect,
  lang,
}: {
  entry: Entry
  ids: Set<string>
  onSelect: (id: string) => void
  lang: string
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const title = pickL(entry.title, entry.title_zh, lang)
  const symptom = pickL(entry.symptom, entry.symptom_zh, lang)
  const trustNote = pickL(entry.trust_note, entry.trust_note_zh, lang)
  const communityBody = pickL(entry.community_body, entry.community_body_zh, lang)
  const ifStuckNote = pickL(entry.if_stuck?.note, entry.if_stuck?.note_zh, lang)
  const ifStuck = ifStuckNote || entry.if_stuck?.text || ''

  // Send the crew prompt to a fresh dashboard chat (same intent slot ChatPage
  // consumes as useChatLauncher; replicated here so this builtin needs no
  // AppApiProvider).
  const sendToChat = useCallback(
    (message: string) => {
      ;(window as Window & { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = {
        message,
        ts: Date.now(),
      }
      navigate('/chat')
    },
    [navigate],
  )

  return (
    <div className="max-w-2xl">
      <div className="flex items-center gap-2 mb-1">
        {entry.community ? (
          <span
            className="inline-flex items-center gap-1 text-xs font-medium rounded-full px-2 py-0.5"
            style={{ color: 'var(--accent)', border: '1px solid color-mix(in srgb, var(--accent) 45%, transparent)' }}
          >
            <Users size={12} />
            {t('apps.guide.communityBadge')}
          </span>
        ) : (
          <TrustBadge trust={entry.trust} />
        )}
      </div>
      <h1 className="text-xl font-semibold">{title}</h1>

      {symptom && (
        <>
          <h2 className="text-xs font-semibold uppercase tracking-wide mt-5 mb-1" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.symptomLabel')}
          </h2>
          <div className="text-sm">
            <MdInline text={symptom} ids={ids} onSelect={onSelect} />
          </div>
        </>
      )}

      {trustNote && (
        <div
          className="text-sm mt-3 rounded-lg p-3"
          style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
        >
          <MdInline text={trustNote} ids={ids} onSelect={onSelect} />
        </div>
      )}

      {communityBody && (
        <div className="text-sm mt-3">
          <MdInline text={communityBody} ids={ids} onSelect={onSelect} />
          {entry.community_author && (
            <div className="text-xs mt-2" style={{ color: 'var(--muted)' }}>
              {entry.community_permalink ? (
                <a
                  href={entry.community_permalink}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
                  style={{ color: 'var(--accent)' }}
                >
                  — {entry.community_author}
                  {entry.community_date ? `, ${entry.community_date}` : ''}
                </a>
              ) : (
                <span>
                  — {entry.community_author}
                  {entry.community_date ? `, ${entry.community_date}` : ''}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {entry.steps && entry.steps.length > 0 && (
        <>
          <h2 className="text-xs font-semibold uppercase tracking-wide mt-5 mb-2" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.stepsLabel')}
          </h2>
          <ol className="flex flex-col gap-3">
            {entry.steps.map((s, i) => (
              <StepBlock key={i} step={s} ids={ids} onSelect={onSelect} lang={lang} />
            ))}
          </ol>
        </>
      )}

      {ifStuck && (
        <>
          <h2 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide mt-5 mb-1" style={{ color: 'var(--muted)' }}>
            <LifeBuoy size={13} />
            {t('apps.guide.ifStuckLabel')}
          </h2>
          <div
            className="text-sm rounded-lg p-3"
            style={{ border: '1px solid color-mix(in srgb, var(--warn) 45%, transparent)' }}
          >
            <MdInline text={ifStuck} ids={ids} onSelect={onSelect} />
          </div>
        </>
      )}

      {entry.crew_prompt && (
        <>
          <h2 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide mt-5 mb-1" style={{ color: 'var(--muted)' }}>
            <Wrench size={13} />
            {t('apps.guide.crewPromptLabel')}
          </h2>
          <p
            className="text-sm rounded-lg p-3 font-mono"
            style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
          >
            {entry.crew_prompt}
          </p>
          <div className="flex items-center gap-2 mt-2">
            <CopyButton text={entry.crew_prompt} />
            <button
              type="button"
              onClick={() => sendToChat(entry.crew_prompt as string)}
              className="inline-flex items-center gap-1 text-xs rounded px-2 py-1 focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
              style={{ color: 'var(--accent)', border: '1px solid color-mix(in srgb, var(--accent) 45%, transparent)' }}
            >
              <MessageSquarePlus size={13} />
              {t('apps.guide.sendToChat')}
            </button>
          </div>
        </>
      )}

      {entry.sources && entry.sources.length > 0 && (
        <>
          <h2 className="text-xs font-semibold uppercase tracking-wide mt-5 mb-1" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.sourcesLabel')}
          </h2>
          <ul className="flex flex-col gap-1">
            {entry.sources.map((s, i) => (
              <li key={i} className="text-sm flex items-center gap-1.5">
                <Link2 size={13} style={{ color: 'var(--muted)' }} />
                {s.url ? (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
                    style={{ color: 'var(--accent)' }}
                  >
                    {s.label || s.url}
                  </a>
                ) : (
                  <span>{s.label}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
