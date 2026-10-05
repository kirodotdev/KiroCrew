import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  defaultMessageRenderers,
  mergeRenderers,
  resolveRenderer,
  type MessageRenderContext,
} from '../app-sdk/messageRenderers'
import SkillLoadCard, {
  isSkillLoadRow,
  readSkillLoad,
} from '../pages/chat/SkillLoadCard'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'

const message = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  role: 'system',
  content: 'Loaded skill(s) via `$`: **explain-for**',
  cls: '',
  ...over,
})

const loaded = message({
  meta: {
    kind: 'skill_load',
    skills: [
      {
        name: 'explain-for',
        body: '---\n# Explain for\n\n- Start with the audience\n- Use `plain words`\n\n---\n\n## Steps\n\nKeep both sections.',
      },
    ],
  },
})

const multipleLoaded = message({
  meta: {
    kind: 'skill_load',
    skills: [
      { name: 'first', body: '# First\nONE' },
      { name: 'second', body: '# Second\nTWO' },
    ],
  },
})

const context = (messages: ChatMessage[]): MessageRenderContext => ({
  index: 0,
  messages,
  running: false,
  key: 'skill-row',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: children => children,
  row: children => children,
})

describe('readSkillLoad', () => {
  it('validates the structured body snapshot without altering its markdown', () => {
    expect(readSkillLoad(loaded)).toEqual([
      {
        name: 'explain-for',
        body: '---\n# Explain for\n\n- Start with the audience\n- Use `plain words`\n\n---\n\n## Steps\n\nKeep both sections.',
      },
    ])
  })

  it('recovers names from a legacy notice without inventing unavailable bodies', () => {
    expect(readSkillLoad(message({
      content: '\u{1F4CE} Loaded skill(s) via `$`: **first, second**',
    }))).toEqual([
      { name: 'first', body: '' },
      { name: 'second', body: '' },
    ])
  })
})

describe('SkillLoadCard', () => {
  it('uses the singular title and book icon for one loaded skill', () => {
    const { container } = render(<SkillLoadCard message={loaded} />)

    expect(screen.getByText('Loaded skill')).toBeInTheDocument()
    expect(screen.queryByText('Loaded skills')).not.toBeInTheDocument()
    expect(container.querySelector('.lucide-book-open')).not.toBeNull()
    expect(container.querySelector('.lucide-paperclip')).toBeNull()
  })

  it('is collapsed by default and expands through a native button', () => {
    const { container } = render(<SkillLoadCard message={loaded} disclosureKey="skill-row" />)

    const card = screen.getByTestId('skill-load-card')
    expect(card).toHaveAttribute('data-expanded', 'false')
    expect(screen.getByText('Loaded skill')).toBeInTheDocument()
    expect(screen.getByText('explain-for')).toBeInTheDocument()
    expect(screen.queryByTestId('skill-load-card-body')).not.toBeInTheDocument()
    expect(container.textContent).not.toContain('Start with the audience')

    const toggle = screen.getByRole('button', { name: /Loaded skill/i })
    expect(toggle.tagName).toBe('BUTTON')
    expect(toggle).toHaveAttribute('type', 'button')
    expect(toggle).toHaveClass('hover:bg-bg-hover')
    fireEvent.click(toggle)

    expect(card).toHaveAttribute('data-expanded', 'true')
    const body = screen.getByTestId('skill-load-card-body')
    expect(body.querySelector('.skill-load-markdown')).not.toBeNull()
    expect(screen.getAllByText('Skill instructions the agent followed this turn')).toHaveLength(1)
    expect(body.querySelector('h1')).toHaveTextContent('Explain for')
    expect(body.querySelector('code')).toHaveTextContent('plain words')
    expect(screen.getByRole('heading', { name: 'Steps' })).toBeInTheDocument()
    expect(body).toHaveTextContent('Keep both sections.')
    expect(body.textContent).not.toContain('description: explain clearly')
  })

  it('renders two collapsed skill rows when the outer card opens', () => {
    render(<SkillLoadCard message={multipleLoaded} disclosureKey="skill-row" />)

    expect(screen.getByText('Loaded skills')).toBeInTheDocument()
    expect(screen.getByText('first, second')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'first' })).not.toBeInTheDocument()
    expect(screen.queryByText('ONE')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Loaded skills/i }))

    const outerBody = screen.getByTestId('skill-load-card-body')
    expect(outerBody).not.toHaveClass('overflow-y-auto')
    expect(outerBody.className).not.toMatch(/max-h-\[/)
    expect(screen.getAllByText('Skill instructions the agent followed this turn')).toHaveLength(1)
    const firstToggle = screen.getByRole('button', { name: 'first' })
    const secondToggle = screen.getByRole('button', { name: 'second' })
    expect(firstToggle).toHaveAttribute('type', 'button')
    expect(firstToggle).toHaveAttribute('aria-expanded', 'false')
    expect(firstToggle).toHaveAttribute('aria-controls')
    expect(secondToggle).toHaveAttribute('type', 'button')
    expect(secondToggle).toHaveAttribute('aria-expanded', 'false')
    expect(secondToggle).toHaveAttribute('aria-controls')
    expect(screen.queryByText('ONE')).not.toBeInTheDocument()
    expect(screen.queryByText('TWO')).not.toBeInTheDocument()
  })

  it('opens only the selected skill body and keeps the other row operable', () => {
    render(<SkillLoadCard message={multipleLoaded} disclosureKey="skill-row" />)
    fireEvent.click(screen.getByRole('button', { name: /Loaded skills/i }))

    const firstToggle = screen.getByRole('button', { name: 'first' })
    const secondToggle = screen.getByRole('button', { name: 'second' })
    fireEvent.click(firstToggle)

    expect(firstToggle).toHaveAttribute('aria-expanded', 'true')
    const firstBody = screen.getByRole('region', { name: 'first' })
    expect(firstBody).toHaveTextContent('ONE')
    expect(firstBody.className).toMatch(/max-h-\[/)
    expect(firstBody).toHaveClass('overflow-y-auto', 'overflow-x-hidden', 'min-w-0')
    expect(firstBody).toHaveAttribute('tabindex', '0')
    expect(firstBody).toHaveAttribute('id', firstToggle.getAttribute('aria-controls'))
    expect(screen.queryByText('TWO')).not.toBeInTheDocument()
    expect(secondToggle).toBeVisible()
    expect(secondToggle).toBeEnabled()

    fireEvent.click(secondToggle)
    expect(secondToggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('region', { name: 'second' })).toHaveTextContent('TWO')
  })

  it('keeps an empty skill body static beside a saved skill body', () => {
    const mixed = message({
      meta: {
        kind: 'skill_load',
        skills: [
          { name: 'saved-skill', body: '# Saved\nSAVED BODY' },
          { name: 'empty-skill', body: '' },
        ],
      },
    })
    render(<SkillLoadCard message={mixed} disclosureKey="mixed-row" />)
    fireEvent.click(screen.getByRole('button', { name: /Loaded skills/i }))

    expect(screen.getByRole('button', { name: 'saved-skill' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('button', { name: 'empty-skill' })).not.toBeInTheDocument()
    expect(screen.getByText('empty-skill')).toBeInTheDocument()
    expect(screen.getByText('This skill has no instructions')).toBeInTheDocument()
    expect(screen.queryByText("Instructions weren't recorded for older turns")).not.toBeInTheDocument()
    expect(screen.queryByText('SAVED BODY')).not.toBeInTheDocument()
  })

  it('shows and clears the bottom scroll cue for a capped body', () => {
    const long = message({
      meta: {
        kind: 'skill_load',
        skills: [{ name: 'long-skill', body: `# Long\n${'line\n'.repeat(20_000)}` }],
      },
    })
    render(<SkillLoadCard message={long} />)
    fireEvent.click(screen.getByRole('button', { name: /Loaded skill/i }))

    const body = screen.getByTestId('skill-load-card-body')
    expect(body.className).toMatch(/max-h-\[/)
    expect(body).toHaveClass('overflow-y-auto', 'overflow-x-hidden', 'min-w-0')
    expect(body).toHaveAttribute('tabindex', '0')

    Object.defineProperties(body, {
      clientHeight: { configurable: true, value: 384 },
      scrollHeight: { configurable: true, value: 768 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    fireEvent.scroll(body)
    expect(body).toHaveClass('markdown-disclosure-scroll-more')
    expect(body).toHaveAttribute('data-scroll-more', '')

    body.scrollTop = 384
    fireEvent.scroll(body)
    expect(body).not.toHaveClass('markdown-disclosure-scroll-more')
    expect(body).not.toHaveAttribute('data-scroll-more')
  })

  it('renders a legacy multi-skill notice as a finished non-interactive card', () => {
    const legacy = message({
      content: '\u{1F4CE} Loaded skill(s) via `$`: **old-skill, second-old-skill**',
    })
    render(<SkillLoadCard message={legacy} />)

    expect(screen.getByText('Loaded skills')).toBeInTheDocument()
    expect(screen.getByText('old-skill, second-old-skill')).toBeInTheDocument()
    expect(screen.getByText("Instructions weren't recorded for older turns")).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skill-load-card-body')).not.toBeInTheDocument()
  })
})

describe('loaded-skill renderer registration', () => {
  it('matches only structured or legacy skill-load system rows', () => {
    expect(isSkillLoadRow(loaded)).toBe(true)
    expect(isSkillLoadRow(message({ content: '\u{1F4CE} Loaded skill(s) via `$`: **old**' }))).toBe(true)
    expect(isSkillLoadRow(message({ content: 'ordinary system row' }))).toBe(false)
    expect(isSkillLoadRow(message({ role: 'assistant', meta: loaded.meta }))).toBe(false)
  })

  it('resolves through both dashboard and SDK registries ahead of generic system rows', () => {
    expect(resolveRenderer(loaded, defaultMessageRenderers)?.id).toBe('skill_load')
    const dashboard = mergeRenderers(createTranscriptRenderers({ slot: 's1' }))
    expect(resolveRenderer(loaded, dashboard)?.id).toBe('skill_load')

    const entry = resolveRenderer(loaded, dashboard)!
    const { container } = render(<>{entry.render(loaded, context([loaded]))}</>)
    expect(container.querySelector('[data-testid="skill-load-card"]')).not.toBeNull()
  })
})
