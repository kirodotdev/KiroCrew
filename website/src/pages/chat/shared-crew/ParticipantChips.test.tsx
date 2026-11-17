import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type { PublicPrincipal, SessionAttribution } from '../../../api/sharedCrew'
import { ParticipantChips, TeamParticipantChips, MAX_PARTICIPANT_CHIPS, participantTitle } from './ParticipantChips'
import { SessionCreatorByline, SharedCrewBylineContext } from '../../chat-sidebar/SessionCreatorByline'

const team = vi.hoisted(() => ({ on: true }))
vi.mock('../../../hooks/sharedCrew/useSharedCrew', () => ({ useTeamAttribution: () => team.on }))

const person = (id: string, name: string, login = ''): PublicPrincipal => ({ id, kind: 'human', display: { name, login, captured_at: null } })

const PRIYA = person('p_priya', 'Priya Rao', 'priya@example.com')
const SAM = person('p_sam', 'Sam Avila')
const LEE = person('p_lee', 'lee@example.com', 'lee@example.com')
const KIM = person('p_kim', 'Kim Ng')
const JO = person('p_jo', 'Jo Diaz')

beforeEach(() => { team.on = true })

describe('ParticipantChips', () => {
  it('draws one initials chip per person in the order given, each named by the full display name', () => {
    render(<ParticipantChips participants={[PRIYA, SAM, LEE]} testId="t" />)
    const chips = within(screen.getByTestId('t')).getAllByRole('img')
    expect(chips.map(c => c.textContent)).toEqual(['PR', 'SA', 'LE'])
    expect(chips.map(c => c.getAttribute('aria-label'))).toEqual(['Priya Rao', 'Sam Avila', 'lee@example.com'])
    // The hover title adds the login only when it says something more.
    expect(chips[0]).toHaveAttribute('title', 'Priya Rao (priya@example.com)')
    expect(chips[2]).toHaveAttribute('title', 'lee@example.com')
    // Inside a row that is itself the control, no chip is a tab stop.
    for (const chip of chips) expect(chip).not.toHaveAttribute('tabindex')
  })

  it('folds everyone past the cap into a +N chip that names them on hover', () => {
    render(<ParticipantChips participants={[PRIYA, SAM, LEE, KIM, JO]} testId="t" />)
    const group = screen.getByTestId('t')
    expect(within(group).getAllByRole('img')).toHaveLength(MAX_PARTICIPANT_CHIPS + 1)
    const more = screen.getByTestId('t-more')
    expect(more).toHaveTextContent('+2')
    expect(more.getAttribute('aria-label')).toMatch(/2/)
    expect(more.getAttribute('title')).toContain(participantTitle(KIM))
    expect(more.getAttribute('title')).toContain(participantTitle(JO))
  })

  it('counts the people the gateway dropped past its cap into +N', () => {
    render(<ParticipantChips participants={[PRIYA, SAM, LEE, KIM]} dropped={5} testId="t" />)
    expect(screen.getByTestId('t-more')).toHaveTextContent('+6')
    expect(screen.getByTestId('t').getAttribute('aria-label')).toMatch(/9/)
  })

  it('draws its letters at the 10px minimum', () => {
    render(<ParticipantChips participants={[PRIYA, SAM, LEE, KIM]} testId="t" />)
    for (const chip of within(screen.getByTestId('t')).getAllByRole('img')) expect(chip.className).toContain('text-[10px]')
  })

  it('draws no +N chip at the cap, and nothing for nobody', () => {
    const { container } = render(<ParticipantChips participants={[]} testId="empty" />)
    expect(container).toBeEmptyDOMElement()
    render(<ParticipantChips participants={[PRIYA, SAM, LEE]} testId="t" />)
    expect(screen.queryByTestId('t-more')).toBeNull()
  })
})

describe('session row: the creator byline rule', () => {
  it('shows the chips on a team Crew, even before a creator is known', () => {
    render(
      <SharedCrewBylineContext.Provider value={{ hidden: false, viewerId: '' }}>
        <SessionCreatorByline attribution={null} participants={[PRIYA, SAM]} />
      </SharedCrewBylineContext.Provider>,
    )
    expect(within(screen.getByTestId('session-participants')).getAllByRole('img')).toHaveLength(2)
  })

  it('leaves the named creator out of the chips, which then read as the others', () => {
    const attribution = { creator: PRIYA, trigger: null } as unknown as SessionAttribution
    render(
      <SharedCrewBylineContext.Provider value={{ hidden: false, viewerId: '' }}>
        <SessionCreatorByline attribution={attribution} participants={[PRIYA, SAM]} />
      </SharedCrewBylineContext.Provider>,
    )
    const group = screen.getByTestId('session-participants')
    expect(within(group).getAllByRole('img').map(c => c.textContent)).toEqual(['SA'])
    expect(group.getAttribute('title')).toBe(group.getAttribute('aria-label'))
    expect(group.getAttribute('title')).toMatch(/1/)
  })

  it('draws nothing on a personal Crew', () => {
    const { container } = render(
      <SharedCrewBylineContext.Provider value={{ hidden: true, viewerId: '' }}>
        <SessionCreatorByline attribution={null} participants={[PRIYA, SAM]} />
      </SharedCrewBylineContext.Provider>,
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('Bot row: TeamParticipantChips', () => {
  it('shows the chips only on a team Crew', () => {
    const { rerender } = render(<TeamParticipantChips participants={[PRIYA]} testId="bot" />)
    expect(screen.getByTestId('bot')).toBeInTheDocument()
    // The group says what the chips are on hover, not only to a screen reader.
    expect(screen.getByTestId('bot').getAttribute('title')).toMatch(/1/)
    team.on = false
    rerender(<TeamParticipantChips participants={[SAM]} testId="bot" />)
    expect(screen.queryByTestId('bot')).toBeNull()
  })

  it('draws nothing for a row with no participants (a relay teammate roster carries none)', () => {
    const { container } = render(<TeamParticipantChips participants={undefined} testId="bot" />)
    expect(container).toBeEmptyDOMElement()
  })
})
