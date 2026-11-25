import { describe, expect, it } from 'vitest'
import { CARD_KINDS } from '../api/cards'
import { i18nT } from '../i18n/t'
import { specFor } from './cardRegistry'
import { fillPath } from './runPlan'

describe('owner-approved Knowledge import', () => {
  it('renders an actionable card using the Knowledge page wording', () => {
    expect(CARD_KINDS).toContain('knowledge.import_sources')
    const spec = specFor('knowledge.import_sources')!
    expect(spec).not.toBeNull()
    expect(spec.label()).toBe(i18nT('pages.knowledge.index.knowledge_library'))
    expect(spec.primary()).toBe(i18nT('pages.knowledge.sourcesList.confirm_scan'))
  })

  it('confirms only the actual source returned by the preceding add step', () => {
    const confirm = {
      method: 'POST', path: '/api/knowledge/sources/{{step0.id}}/confirm',
      fill: [{ field: 'id', source: 'step' as const, step: 0, key: 'id', in: 'path' as const }],
    }
    expect(fillPath(confirm, [{ id: 'source-123' }])).toBe('/api/knowledge/sources/source-123/confirm')
  })
})
