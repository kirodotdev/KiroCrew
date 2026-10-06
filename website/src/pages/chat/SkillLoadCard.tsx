import { memo, useId } from 'react'
import { BookOpen, ChevronRight } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'
import MarkdownDisclosureCard, { MarkdownDisclosureBody } from './MarkdownDisclosureCard'
import { useRowDisclosure } from './rowDisclosure'

export interface LoadedSkillSnapshot {
  name: string
  body: string
}

// Wire value from gateways that predate the structured skill_load metadata.
// Keep the English exact. It is parsed into catalog-backed card copy and never
// rendered directly by this component.
const LEGACY_SKILL_LOAD_RE = /^\s*\u{1F4CE}\s+Loaded skill\(s\) via `\$`:\s+\*\*(.+?)\*\*\s*$/u

function legacyNames(content: string): LoadedSkillSnapshot[] {
  const match = LEGACY_SKILL_LOAD_RE.exec(content)
  if (!match) return []
  return match[1]
    .split(',')
    .map(name => name.trim())
    .filter(Boolean)
    .map(name => ({ name, body: '' }))
}

/** Validate the untrusted transcript metadata before rendering it. */
export function readSkillLoad(message: Pick<ChatMessage, 'content' | 'meta'>): LoadedSkillSnapshot[] {
  const raw = message.meta?.skills
  if (!Array.isArray(raw)) return legacyNames(message.content)

  const snapshots = raw.flatMap(item => {
    if (!item || typeof item !== 'object') return []
    const name = (item as { name?: unknown }).name
    const body = (item as { body?: unknown }).body
    if (typeof name !== 'string' || !name.trim() || typeof body !== 'string') return []
    return [{ name: name.trim(), body }]
  })
  return snapshots.length > 0 ? snapshots : legacyNames(message.content)
}

/** True for the structured row and for the exact legacy notice shape. */
export function isSkillLoadRow(
  message: Pick<ChatMessage, 'role' | 'content' | 'kind' | 'meta'>,
): boolean {
  if (message.role !== 'system') return false
  const kind = message.kind ?? message.meta?.kind
  return kind === 'skill_load' || LEGACY_SKILL_LOAD_RE.test(message.content)
}

function SkillDisclosureRow({
  skill,
  index,
  disclosureKey,
  emptyBodyText,
}: {
  skill: LoadedSkillSnapshot
  index: number
  disclosureKey?: string
  emptyBodyText: string
}) {
  const rowKey = disclosureKey ? `${disclosureKey}:skill:${index}` : undefined
  const [expanded, setExpanded] = useRowDisclosure(rowKey, false)
  const headlineId = useId()
  const bodyId = useId()
  const hasBody = skill.body.length > 0

  if (!hasBody) {
    return (
      <div className="min-w-0 border-t border-border px-2 py-2">
        <span className="font-medium text-text break-words" translate="no">{skill.name}</span>
        <span className="mx-1.5" aria-hidden="true">·</span>
        <span className="text-[12px] opacity-75">
          {emptyBodyText}
        </span>
      </div>
    )
  }

  return (
    <div className="min-w-0 border-t border-border">
      <Btn
        type="button"
        onClick={() => setExpanded(value => !value)}
        aria-expanded={expanded}
        aria-controls={bodyId}
        className="w-full justify-start gap-2 px-2 py-2 rounded-sm border-0 text-left text-[13px] leading-5 bg-transparent text-inherit hover:bg-bg-hover hover:border-transparent active:scale-100"
      >
        <ChevronRight
          size={13}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <span id={headlineId} className="font-medium text-text break-words min-w-0" translate="no">
          {skill.name}
        </span>
      </Btn>
      {expanded && (
        <MarkdownDisclosureBody
          id={bodyId}
          labelledBy={headlineId}
          testId={`skill-load-card-body-${index}`}
        >
          <div className="skill-load-markdown min-w-0">
            <MarkdownRenderer content={skill.body} />
          </div>
        </MarkdownDisclosureBody>
      )}
    </div>
  )
}

export default memo(function SkillLoadCard({
  message,
  disclosureKey,
}: {
  message: ChatMessage
  disclosureKey?: string
}) {
  useLanguageGeneration()
  const skills = readSkillLoad(message)
  if (skills.length === 0) return null

  const hasStructuredSnapshots = Array.isArray(message.meta?.skills)
  const emptyBodyText = i18nT(
    hasStructuredSnapshots
      ? 'pages.chat.skillLoadCard.body_empty'
      : 'pages.chat.skillLoadCard.body_unavailable',
  )
  const bodies = skills.filter(skill => skill.body.length > 0)
  const names = skills.map(skill => skill.name).join(', ')
  const body = bodies.length > 0 ? (
    <div className="min-w-0">
      <p className={`${skills.length === 1 ? 'mb-3' : 'mb-1'} text-[12px] leading-5 text-muted`}>
        {i18nT('pages.chat.skillLoadCard.body_context')}
      </p>
      {skills.length === 1 ? (
        <div className="skill-load-markdown min-w-0 space-y-4">
          <section className="min-w-0" aria-label={skills[0].name}>
            <MarkdownRenderer content={skills[0].body} />
          </section>
        </div>
      ) : (
        <div className="min-w-0">
          {skills.map((skill, index) => (
            <SkillDisclosureRow
              key={`${skill.name}-${index}`}
              skill={skill}
              index={index}
              disclosureKey={disclosureKey}
              emptyBodyText={emptyBodyText}
            />
          ))}
        </div>
      )}
    </div>
  ) : undefined

  return (
    <MarkdownDisclosureCard
      icon={BookOpen}
      title={i18nT('pages.chat.skillLoadCard.title', { count: skills.length })}
      detail={({ hasBody }) => (
        <>
          <span translate="no">{names}</span>
          {!hasBody && (
            <>
              <span className="mx-1.5" aria-hidden="true">·</span>
              <span>{emptyBodyText}</span>
            </>
          )}
        </>
      )}
      body={body}
      bodyScrollable={skills.length === 1}
      disclosureKey={disclosureKey}
      testId="skill-load-card"
      bodyTestId="skill-load-card-body"
      status="loaded"
    />
  )
})
