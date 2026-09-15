/**
 * Tests for the remote crews pane.
 *
 * Five behaviours, each of which fails silently if it regresses:
 *
 *   - The THREE non-error states are three different answers. A missing base
 *     stack, an account with no crews, and an unknown memory mode all render
 *     without an error, and collapsing any two of them into one sentence tells
 *     the owner to do the wrong thing.
 *   - An unknown mode must never read as `chatbot`. The backend returns empty on
 *     purpose so this pane can say it does not know; a default would state a fact
 *     about a live deployment that nothing was read from.
 *   - A row stays aligned when one card carries a status badge and its neighbour
 *     does not. That is the whole reason the header block has a fixed height, and
 *     it is invisible in a passing render without measuring it.
 *   - `account_mismatch` gets its own copy. It is not a transient failure: the
 *     profile now signs in to a DIFFERENT AWS account, and the fix is a
 *     reconnect, not a retry.
 *
 * jsdom reports every layout box as zero, so the alignment case asserts on the
 * fixed-height CLASS the cards share rather than on measured pixels: that class
 * is the mechanism, and a real measured render is in
 * `scripts/capture-aws-control-crews.mjs`, which fails when the fact grids of two
 * cards in a row do not start on the same line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type { CrewMemoryMode, CrewsResponse, RemoteCrew } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: { crews: vi.fn(), crew: vi.fn() },
  }
})

import { awsControlApi, AwsControlError } from './api'
import { CrewsPane } from './CrewsPage'

const ACCOUNT = '111122223333'

/**
 * A digest-pinned image, the only form the template accepts: its `AllowedPattern`
 * is `.+@sha256:[a-f0-9]{64}$`, so a fixture with a short fake digest would
 * understate the length the card has to cope with.
 */
const DIGEST = '9f1c2d3e4b5a67788990aabbccddeeff00112233445566778899aabbccddeeff'
const IMAGE = `111122223333.dkr.ecr.us-west-2.amazonaws.com/smc@sha256:${DIGEST}`

/** A settled crew, the shape the LIST route answers with. */
function crew(over: Partial<RemoteCrew> = {}): RemoteCrew {
  const name = over.name ?? 'support'
  return {
    name,
    // Derived the way `crews.py` parses it back out, so a test can tell a real
    // per-crew stack from a constant.
    stack: `smc-crew-${name}`,
    stackStatus: 'CREATE_COMPLETE',
    memory: 'chatbot',
    // The list route makes no ECS call, so these three arrive empty and zero.
    // Every fixture here keeps that honest rather than filling them in.
    service: '',
    running: 0,
    desired: 0,
    image: IMAGE,
    controlBase: 'https://d1abcd.cloudfront.net',
    region: 'us-west-2',
    ...over,
  }
}

function inventory(over: Partial<CrewsResponse> = {}): CrewsResponse {
  return { baseMissing: false, crews: [], ...over }
}

const listMock = () => vi.mocked(awsControlApi.crews)
const detailMock = () => vi.mocked(awsControlApi.crew)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('the three states that are not errors', () => {
  it('says what is MISSING when the base stack is absent, not that there are no crews', async () => {
    // "No crews" would be true and useless: nothing the owner does to a crew
    // fixes this, and the sentence has to name the thing that is not there.
    listMock().mockResolvedValue(inventory({ baseMissing: true }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getByTestId('crews-base-missing')).toBeTruthy())
    expect(screen.getByText(i18nT('apps.awsControl.crews.base_missing_title'))).toBeTruthy()
    // The empty state instructs but must not be a dead end: it carries the same
    // agent hand-off the error notices on this pane do, so a reader with nothing
    // to click here can still ask the agent about it.
    expect(within(screen.getByTestId('crews-base-missing')).getByTestId('crews-base-missing-action'))
      .toBeTruthy()
    expect(screen.getByTestId('crews-base-missing-action'))
      .toHaveTextContent(i18nT('components.askAgent.ask_the_agent'))
    // Emphatically NOT the empty-account state, and no error surface either.
    expect(screen.queryByTestId('crews-empty')).toBeNull()
    expect(screen.queryByTestId('crews-error')).toBeNull()
    expect(screen.queryByTestId('crews-grid')).toBeNull()
  })

  it('says the account is ready and holds none when the base is there', async () => {
    listMock().mockResolvedValue(inventory({ crews: [] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getByTestId('crews-empty')).toBeTruthy())
    expect(screen.getByText(i18nT('apps.awsControl.crews.empty_title'))).toBeTruthy()
    expect(screen.queryByTestId('crews-base-missing')).toBeNull()
    // Same hand-off affordance as the base-missing state: an empty state that only
    // instructs, with nothing to click, is the UX finding this addresses.
    expect(screen.getByTestId('crews-empty-action'))
      .toHaveTextContent(i18nT('components.askAgent.ask_the_agent'))
    // The reworded body names the chat, not "your crew" - the pane's own noun is
    // "crews", so telling the reader to "ask your crew" collided with it.
    expect(i18nT('apps.awsControl.crews.empty_body')).not.toContain('your crew')
    // The two empty states must not share a sentence - that is the whole point
    // of having two.
    expect(i18nT('apps.awsControl.crews.empty_title'))
      .not.toBe(i18nT('apps.awsControl.crews.base_missing_title'))
    expect(i18nT('apps.awsControl.crews.empty_body'))
      .not.toBe(i18nT('apps.awsControl.crews.base_missing_body'))
  })

  it('reads an empty memory mode as unknown, never as chatbot', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew({ name: 'legacy', memory: '' })] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    const cell = await waitFor(() => screen.getByTestId('crew-mode'))
    expect(cell).toHaveTextContent(i18nT('apps.awsControl.crews.mode_unknown'))
    // The regression this guards: a `memory || 'chatbot'` default anywhere on the
    // path would render the chatbot label here and assert something about
    // someone's live deployment that no AWS call ever answered.
    expect(cell).not.toHaveTextContent(i18nT('apps.awsControl.crews.mode_chatbot'))
    expect(cell).not.toHaveTextContent(i18nT('apps.awsControl.crews.mode_persistent'))
    // Unknown is not data: it renders italic and muted, the same way an inherited
    // value does on the agents card, so it cannot be misread as a mode named
    // "Unknown".
    const value = cell.querySelector('span')
    expect(value?.className).toContain('italic')
  })

  it('names ANY mode it does not recognise rather than rendering an empty cell', async () => {
    // `CrewMemoryMode` is a closed union, so nothing in the type system produces
    // these. A live stack does: a `Memory` parameter value added by a newer backend,
    // or edited by hand, arrives here as a string this build was never taught. The
    // map lookup then misses and `i18nT(undefined)` renders NOTHING, which on a
    // deployment page is indistinguishable from a row that failed to load.
    //
    // This is a PROPERTY over the whole class of unrecognised values, not one pinned
    // string: the defect the earlier capture harness had was exactly a check bound to
    // a single instance (it clicked one crew id and so could never photograph a
    // degraded state), so pinning `'ephemeral'` alone would leave a fallback keyed to
    // that one word passing while every other unknown value still rendered blank.
    const unknownModes = [
      'ephemeral',
      'hybrid',
      'v2',
      'RAM',
      'a wholly novel mode',
      'PERSISTENT ', // trailing space: not the known `persistent`, so still unknown
      'Chatbot', // different case: not the known lowercase `chatbot`
    ]
    for (const mode of unknownModes) {
      vi.clearAllMocks()
      listMock().mockResolvedValue(
        inventory({ crews: [crew({ name: 'future', memory: mode as CrewMemoryMode })] }),
      )
      const { unmount } = renderWithProviders(<CrewsPane account={ACCOUNT} />)

      const cell = await waitFor(() => screen.getByTestId('crew-mode'))
      // Every unrecognised value renders the SAME "Unknown", never blank.
      expect(cell, `mode ${JSON.stringify(mode)} should render Unknown`)
        .toHaveTextContent(i18nT('apps.awsControl.crews.mode_unknown'))
      // Never the raw wire value: echoing it would present a word this build cannot
      // define as though the page understood it.
      expect(cell.textContent, `mode ${JSON.stringify(mode)} must not echo the wire value`)
        .not.toContain(mode.trim())
      // And never guessed as one of the two named modes.
      expect(cell).not.toHaveTextContent(i18nT('apps.awsControl.crews.mode_chatbot'))
      expect(cell).not.toHaveTextContent(i18nT('apps.awsControl.crews.mode_persistent'))
      // Unknown is not data: italic and muted, so it cannot be misread as a real
      // mode literally named "Unknown".
      const value = cell.querySelector('span')
      expect(value?.className, `mode ${JSON.stringify(mode)} should be italic`).toContain('italic')
      unmount()
    }
  })
})

describe('a crew being deleted', () => {
  it('stays listed and is visibly not healthy', async () => {
    // Filtering it out is how a half-deleted crew becomes a surprise on the next
    // bill, so it is listed on purpose - and then it must not look fine.
    listMock().mockResolvedValue(inventory({
      crews: [crew({ name: 'dying', stackStatus: 'DELETE_IN_PROGRESS' })],
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    const card = await waitFor(() => screen.getByTestId('crew-card'))
    expect(card.getAttribute('data-status')).toBe('DELETE_IN_PROGRESS')
    expect(card).toHaveTextContent(i18nT('apps.awsControl.crews.status_deleting'))
    // The danger tint, not just a word: the card has to read as wrong at a
    // glance in a grid of otherwise-quiet cards.
    expect(card.className).toContain('border-danger/40')
  })

  it('renders no badge at all for a settled stack, so the badge means something', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew({ stackStatus: 'UPDATE_COMPLETE' })] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    const card = await waitFor(() => screen.getByTestId('crew-card'))
    for (const key of [
      'apps.awsControl.crews.status_creating',
      'apps.awsControl.crews.status_updating',
      'apps.awsControl.crews.status_deleting',
      'apps.awsControl.crews.status_delete_failed',
      'apps.awsControl.crews.status_rolled_back',
    ]) {
      expect(card).not.toHaveTextContent(i18nT(key))
    }
    // And the raw AWS token never leaks onto a settled card either.
    expect(card).not.toHaveTextContent('UPDATE_COMPLETE')
  })
})

describe('row alignment', () => {
  it('gives a badge-carrying card the same header height as its bare neighbour', async () => {
    // The failure this pins: a badge is taller than plain text, so a header that
    // sized itself to its content would push one card's fact grid below its
    // neighbour's and the row would read as ragged.
    listMock().mockResolvedValue(inventory({
      crews: [
        crew({ name: 'quiet', stackStatus: 'CREATE_COMPLETE' }),
        crew({ name: 'busy', stackStatus: 'UPDATE_IN_PROGRESS' }),
      ],
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(2))
    const [quiet, busy] = screen.getAllByTestId('crew-card')

    // Exactly one of the two carries a badge, or the case being tested is not
    // the case being rendered.
    expect(busy).toHaveTextContent(i18nT('apps.awsControl.crews.status_updating'))
    expect(quiet).not.toHaveTextContent(i18nT('apps.awsControl.crews.status_updating'))

    // Same fixed height on both header blocks. jsdom measures every box as zero,
    // so the class IS the assertion here; the measured version lives in the
    // capture harness.
    const header = (card: HTMLElement) => within(card).getByTestId('crew-card-header')
    const heightClass = (el: HTMLElement) =>
      el.className.split(/\s+/).filter((c) => /^h-\[/.test(c))
    expect(heightClass(header(quiet))).toEqual(['h-[38px]'])
    expect(heightClass(header(busy))).toEqual(heightClass(header(quiet)))
    // The name line must not be allowed to wrap either: a long name plus a badge
    // would otherwise grow the header past its fixed height and clip instead.
    for (const card of [quiet, busy]) {
      const nameRow = within(card).getByTestId('crew-name').parentElement
      expect(nameRow?.className).toContain('min-w-0')
      expect(within(card).getByTestId('crew-name').className).toContain('truncate')
    }
  })

  it('keeps every card to the same four facts, so the grids line up cell for cell', async () => {
    listMock().mockResolvedValue(inventory({
      crews: [
        crew({ name: 'full' }),
        // Nothing published yet: the endpoint and image are empty, and the cells
        // must still be PRESENT (as "not set") rather than dropped, or this
        // card's grid would be one row short of its neighbour's.
        crew({ name: 'bare', memory: '', controlBase: '', image: '' }),
      ],
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(2))
    for (const card of screen.getAllByTestId('crew-card')) {
      const facts = within(card).getByTestId('crew-card-facts')
      expect(facts.children).toHaveLength(4)
    }
    const bare = screen.getAllByTestId('crew-card')[1]
    expect(within(bare).getByTestId('crew-endpoint'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.unset'))
    expect(within(bare).getByTestId('crew-image'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.unset'))
  })
})

describe('the image reference', () => {
  it('shows the digest on the card, not the registry prefix every crew shares', async () => {
    // The template pins by digest and refuses a tag, so the value is over a
    // hundred characters with the identifying part at the END. Truncating it left
    // to right made every card read
    // `111122223333.dkr.ecr.us-west-2.amazonaws.com/…` - a label with no fact
    // under it, which is the defect this pins.
    listMock().mockResolvedValue(inventory({ crews: [crew()] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    const cell = await waitFor(() => screen.getByTestId('crew-image'))
    expect(cell.textContent).toContain('9f1c2d3e4b5a')
    expect(cell.textContent).not.toContain('dkr.ecr')
    // The `sha256:` prefix goes with the registry: the template's pattern makes it
    // identical on every crew that can exist, so it would spend a third of the
    // cell saying nothing.
    expect(cell.textContent).not.toContain('sha256')
    // Shortened, not silently lost: hover carries the whole reference, and the
    // detail view renders it in full with a copy button.
    expect(cell.getAttribute('title')).toBe(IMAGE)
  })

  it('shows a value with no digest whole, rather than assuming the shape', async () => {
    // `crews.py` returns the stack parameter verbatim. Today the template refuses
    // a tag, but this UI does not get to assume what an older stack put there.
    listMock().mockResolvedValue(inventory({ crews: [crew({ image: 'smc:legacy-build' })] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    const cell = await waitFor(() => screen.getByTestId('crew-image'))
    expect(cell).toHaveTextContent('smc:legacy-build')
  })

  it('renders the whole reference on the detail, with a way to copy it', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew({ name: 'support' })] }))
    detailMock().mockResolvedValue(crew({
      name: 'support', service: 'smc-support', running: 1, desired: 1,
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    await waitFor(() => expect(screen.getByTestId('crew-detail-image')).toBeTruthy())
    expect(screen.getByTestId('crew-detail-image')).toHaveTextContent(IMAGE)
    expect(screen.getByTestId('crew-copy-image')).toBeTruthy()
  })
})

describe('account_mismatch', () => {
  it('gets its own copy rather than the generic error wall', async () => {
    // The profile now resolves to a DIFFERENT account, so the backend refused to
    // report that account's crews. Retrying cannot help, and the generic
    // "couldn't read" sentence would send the owner to press Refresh forever.
    listMock().mockRejectedValue(new AwsControlError('account_mismatch', 409))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getByTestId('crews-mismatch')).toBeTruthy())
    expect(screen.getByText(i18nT('apps.awsControl.crews.mismatch'))).toBeTruthy()
    expect(screen.queryByTestId('crews-error')).toBeNull()
    // No Try-again button: the notice that offers one is the generic read notice,
    // and this failure is not one a retry clears.
    expect(screen.queryByTestId('crews-error-retry')).toBeNull()
    expect(screen.queryByTestId('crews-mismatch-retry')).toBeNull()
    // A failed read is not an empty account.
    expect(screen.queryByTestId('crews-empty')).toBeNull()
    expect(screen.queryByTestId('crews-base-missing')).toBeNull()
    expect(i18nT('apps.awsControl.crews.mismatch'))
      .not.toBe(i18nT('apps.awsControl.crews.load_failed'))
  })

  it('still reports an ordinary AWS failure through the retryable notice', async () => {
    listMock().mockRejectedValue(new AwsControlError('aws_call_failed', 502))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getByTestId('crews-error')).toBeTruthy())
    expect(screen.getByText(i18nT('apps.awsControl.crews.load_failed'))).toBeTruthy()
    expect(screen.getByTestId('crews-error-retry')).toBeTruthy()
    expect(screen.queryByTestId('crews-mismatch')).toBeNull()
    expect(screen.queryByTestId('crews-empty')).toBeNull()
  })
})

describe('opening a crew', () => {
  it('descends to the detail in place and comes back, without a route', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew({ name: 'support' })] }))
    // The DETAIL route is the only one that fetches the serving counts, so this
    // is the only payload where they are populated and a serving badge is shown.
    detailMock().mockResolvedValue(crew({
      name: 'support', service: 'smc-support', running: 2, desired: 2,
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    // The section wrapper renders while the detail is still loading, so waiting
    // on IT would assert against a skeleton. Wait for the resolved facts.
    await waitFor(() => expect(screen.getByTestId('crew-detail-facts')).toBeTruthy())
    expect(detailMock()).toHaveBeenCalledWith(ACCOUNT, 'support')
    expect(screen.getByTestId('crew-detail-tasks')).toHaveTextContent(
      i18nT('apps.awsControl.crews.detail_tasks', { running: '2', desired: '2' }),
    )
    expect(screen.getByTestId('crew-detail-facts'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.serving'))

    fireEvent.click(screen.getByTestId('crew-detail-back'))
    await waitFor(() => expect(screen.getByTestId('crews-grid')).toBeTruthy())
    expect(screen.queryByTestId('crew-detail')).toBeNull()
  })

  it('does not call a crew with zero desired tasks "not serving"', async () => {
    // running equals desired at 0/0, but saying either "Serving" or "Not serving"
    // would name a state that is not one: nothing is meant to be running.
    listMock().mockResolvedValue(inventory({ crews: [crew({ name: 'parked' })] }))
    detailMock().mockResolvedValue(crew({
      name: 'parked', service: 'smc-parked', running: 0, desired: 0,
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    await waitFor(() => expect(screen.getByTestId('crew-detail-idle')).toBeTruthy())
    expect(screen.getByTestId('crew-detail-facts'))
      .not.toHaveTextContent(i18nT('apps.awsControl.crews.not_serving'))
    expect(screen.queryByTestId('crew-detail-tasks')).toBeNull()
  })

  it('explains an unknown mode on the detail instead of leaving an italic word', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew({ name: 'legacy', memory: '' })] }))
    detailMock().mockResolvedValue(crew({
      name: 'legacy', memory: '', service: 'smc-legacy', running: 1, desired: 1,
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    await waitFor(() => expect(screen.getByTestId('crew-detail-mode-why')).toBeTruthy())
    expect(screen.getByTestId('crew-detail-mode-value'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.mode_unknown'))
  })

  it('withholds the explanation from a mode it merely cannot name', async () => {
    // Both states read "Unknown", and only one of them has this reason. The
    // sentence says the stack predates the parameter, which is true of an empty
    // mode and false of a value this build cannot read: showing it there would
    // answer WHY with something nothing established. Unknown may be reported; its
    // cause may not be invented.
    const future = crew({ name: 'future', memory: 'ephemeral' as CrewMemoryMode })
    listMock().mockResolvedValue(inventory({ crews: [future] }))
    detailMock().mockResolvedValue(crew({
      name: 'future', memory: 'ephemeral' as CrewMemoryMode,
      service: 'smc-future', running: 1, desired: 1,
    }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    await waitFor(() => expect(screen.getByTestId('crew-detail-mode-value')).toBeTruthy())
    expect(screen.getByTestId('crew-detail-mode-value'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.mode_unknown'))
    expect(screen.queryByTestId('crew-detail-mode-why')).toBeNull()
  })

  it('says a crew has gone rather than reporting a failure to read it', async () => {
    // A crew that finished deleting between the grid render and the click is
    // gone, not broken, and there is nothing to retry.
    listMock().mockResolvedValue(inventory({
      crews: [crew({ name: 'dying', stackStatus: 'DELETE_IN_PROGRESS' })],
    }))
    detailMock().mockRejectedValue(new AwsControlError('crew_absent', 404))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    fireEvent.click(await waitFor(() => screen.getByTestId('crew-card')))
    await waitFor(() => expect(screen.getByTestId('crew-detail-absent')).toBeTruthy())
    expect(screen.getByText(i18nT('apps.awsControl.crews.absent'))).toBeTruthy()
    expect(screen.queryByTestId('crew-detail-error')).toBeNull()
    expect(screen.queryByTestId('crew-detail-error-retry')).toBeNull()
  })
})

describe('the naming collision with the agents page', () => {
  it('titles the pane "remote crews" and says on screen which kind these are', async () => {
    listMock().mockResolvedValue(inventory({ crews: [crew()] }))
    renderWithProviders(<CrewsPane account={ACCOUNT} />)

    await waitFor(() => expect(screen.getByTestId('crews-grid')).toBeTruthy())
    // The rail can say the short word; a TITLE travels (a screenshot, a link)
    // and has to carry the distinction on its own.
    expect(screen.getByTestId('page-title'))
      .toHaveTextContent(i18nT('apps.awsControl.crews.title'))
    expect(i18nT('apps.awsControl.crews.title')).not.toBe(i18nT('apps.awsControl.rail.crews'))
    // And the blurb names the other kind, rather than trusting "remote" to do it.
    expect(screen.getByTestId('crews-blurb'))
      .toHaveTextContent(i18nT('pages.kiroCrewAgentsPage.agents'))
  })
})
