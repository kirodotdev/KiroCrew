import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const H = vi.hoisted(() => ({
  api: {
    projectGitStatus: vi.fn(),
    projectGitLog: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import GitPanel from '../components/GitPanel'
import { namedCeiling } from './namedCeiling'

const PROJECT = '/workspace/project'

/** One open section, as the Git tab's list renders a repository the user is
 *  looking at. `defaultOpen` so the body (notices, changes, commits) is on
 *  screen without a toggle; every GitPanel is a list section now. */
function mount(defaultOpen = true) {
  // GitPanel's reads set `retry: 1` themselves, which outranks the `retry` here,
  // so a rejected route is asked twice before it surfaces. `retryDelay: 0` (they
  // set none) makes that second ask immediate instead of React Query's real
  // one-second default, so a refusal renders without a wall-clock wait.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GitPanel projectDir={PROJECT} section={{ label: PROJECT, defaultOpen }} />
    </QueryClientProvider>,
  )
}

/** Ceiling for the recovery refetch that follows a refused log read and its retry. */
const LOG_REFETCHED = namedCeiling('LOG_REFETCHED', 5000)

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
  H.api.projectGitStatus.mockReset().mockResolvedValue({
    repo: true,
    repoRoot: PROJECT,
    branch: 'main',
    files: [],
  })
  H.api.projectGitLog.mockReset().mockResolvedValue({ repo: true, commits: [] })
})

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
})

describe('GitPanel repository state', () => {
  it('renders a terminal no-repository state without claiming the tree is clean', async () => {
    H.api.projectGitStatus.mockResolvedValue({ repo: false, files: [] })
    H.api.projectGitLog.mockResolvedValue({ repo: false, commits: [] })

    mount()

    expect(await screen.findByText('Not a Git repository')).toBeInTheDocument()
    expect(screen.getByText(
      'This project folder is not a Git repository. If the repository is in a subfolder, click the folder name below the message box.',
    )).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText(/uncommitted/)).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('renders an operational status failure through ErrorNotice without a health claim', async () => {
    H.api.projectGitStatus.mockRejectedValue(new Error('status unavailable'))

    mount()

    // GitPanel retries a failed status read once before React Query exposes
    // statusError; mount()'s `retryDelay: 0` makes that second read immediate.
    expect(await screen.findByTestId('git-panel-status-error')).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('stacks the hand-off under the status error text for the narrow side panel', async () => {
    // Beside the text, the hand-off takes a column of a ~300px panel and the
    // sentence wraps a few letters per line.
    H.api.projectGitStatus.mockRejectedValue(new Error('status unavailable'))

    mount()

    const notice = await screen.findByTestId('git-panel-status-error')
    const handoff = within(notice).getByRole('button', { name: /ask the agent/i })
    expect(notice.querySelector('.flex-1')?.contains(handoff)).toBe(true)
  })

  it('does not render stale status-derived header data beside a failed refresh', async () => {
    const serverMessage = 'server-only repository status detail'
    const codedFailure = Object.assign(new Error(serverMessage), {
      body: JSON.stringify({ error: serverMessage, code: 'git_status_unavailable' }),
    })
    H.api.projectGitStatus
      .mockResolvedValueOnce({
        repo: true,
        repoRoot: PROJECT,
        branch: 'main',
        ahead: 2,
        behind: 1,
        files: [],
      })
      .mockRejectedValue(codedFailure)

    mount()

    expect(await screen.findByText('No changes or commits to display.')).toBeInTheDocument()
    expect(screen.getByText(/↑2\s*↓1/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    const notice = await screen.findByTestId('git-panel-status-error')
    const localizedStatus = 'Couldn’t read the repository status. Commit history may be out of date.'
    expect(notice).toHaveTextContent(localizedStatus)
    expect(notice).not.toHaveTextContent(serverMessage)
    expect(screen.getAllByText(localizedStatus)).toHaveLength(1)
    expect(screen.getByText('Branch unavailable')).toHaveClass('text-muted')
    expect(screen.queryByText('main')).toBeNull()
    expect(screen.queryByText(/↑2\s*↓1/)).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('keeps the structured status report when the visible error is localized', async () => {
    const serverMessage = 'server-only repository status detail'
    const codedFailure = Object.assign(new Error(serverMessage), {
      body: JSON.stringify({ error: serverMessage, code: 'git_status_unavailable' }),
    })
    recordError({
      source: 'api',
      message: serverMessage,
      status: 503,
      code: 'git_status_unavailable',
      endpoint: '/api/project/git/status',
    })
    H.api.projectGitStatus.mockRejectedValue(codedFailure)
    await i18next.changeLanguage('zh-CN')

    mount()

    const notice = await screen.findByTestId('git-panel-status-error')
    expect(notice).toHaveTextContent('无法读取仓库状态。提交历史可能已过时。')
    expect(notice).not.toHaveTextContent(serverMessage)
    // The status notice's hand-off names the half it carries, so that a second
    // notice stacked beside it is not the same affordance twice. Resolved
    // through the catalog, which keeps this assertion language-agnostic.
    await userEvent.click(
      within(notice).getByRole('button', {
        name: i18next.t('components.gitPanel.ask_agent_changes'),
      }),
    )

    const prompt = consumeChatHandoff()
    expect(prompt).toContain('- Request: /api/project/git/status -> HTTP 503')
    expect(prompt).toContain('- Code: git_status_unavailable')
    expect(prompt).toContain(`- Message: ${serverMessage}`)
  })

  it('does not render stale no-repository data beside a failed refresh', async () => {
    H.api.projectGitStatus
      .mockResolvedValueOnce({ repo: false, files: [] })
      .mockRejectedValue(new Error('refresh unavailable'))
    H.api.projectGitLog.mockResolvedValue({ repo: false, commits: [] })

    mount()

    expect(await screen.findByText('Not a Git repository')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByTestId('git-panel-status-error')).toBeInTheDocument()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText(/This project folder is not a Git repository/)).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
  })

  it('does not render stale changed-file rows beside a failed refresh', async () => {
    H.api.projectGitStatus
      .mockResolvedValueOnce({
        repo: true,
        repoRoot: PROJECT,
        branch: 'feature',
        files: [{ path: 'src/a.ts', status: 'M', staged: false }],
      })
      .mockRejectedValue(new Error('refresh unavailable'))

    mount()

    expect(await screen.findByText('Changes')).toBeInTheDocument()
    expect(screen.getByTitle('src/a.ts')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByTestId('git-panel-status-error')).toBeInTheDocument()
    expect(screen.queryByText('Changes')).toBeNull()
    expect(screen.queryByTitle('src/a.ts')).toBeNull()
  })

  it('keeps the clean-repository label, badge, and empty state', async () => {
    mount()

    expect(await screen.findByText('main')).toBeInTheDocument()
    const clean = screen.getByText('clean')
    expect(clean).toBeInTheDocument()
    // Muted, not the success colour: "clean" is the resting state of most rows
    // in the list, and in green it was the loudest thing in the panel. The
    // uncommitted count keeps its colour (see the dirty test below).
    expect(clean.className).toMatch(/\btext-muted\b/)
    expect(clean.className).not.toMatch(/\b(text-ok|bg-ok\/15)\b/)
    expect(screen.getByText('No changes or commits to display.')).toBeInTheDocument()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })

  it('keeps the dirty-repository label, count, and changed-file row', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      repoRoot: PROJECT,
      branch: 'feature',
      files: [{ path: 'src/a.ts', status: 'M', staged: false }],
    })

    mount()

    expect(await screen.findByText('feature')).toBeInTheDocument()
    const count = screen.getByText('1 uncommitted')
    expect(count).toBeInTheDocument()
    expect(count.className).toMatch(/\btext-warn\b/)
    expect(screen.getByText('Changes')).toBeInTheDocument()
    expect(screen.getByTitle('src/a.ts')).toBeInTheDocument()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })
})

describe('GitPanel capped listing', () => {
  const capped = (n: number, truncated: boolean) => ({
    repo: true,
    repoRoot: PROJECT,
    branch: 'main',
    truncated,
    files: Array.from({ length: n }, (_, i) => ({
      path: `src/f${i}.ts`,
      status: 'M',
      staged: false,
    })),
  })

  it('says the listing was cut short instead of reporting the cap as the total', async () => {
    H.api.projectGitStatus.mockResolvedValue(capped(500, true))

    mount()

    expect(await screen.findByTestId('git-panel-truncated'))
      .toHaveTextContent(i18next.t('components.gitPanel.showing_first', { count: 500 }))
    // The pill must not read a bare "500 uncommitted": that is the cap, and the
    // real number is larger by an unknown amount.
    expect(screen.getByText('500+ uncommitted')).toBeInTheDocument()
    expect(screen.queryByText('500 uncommitted')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    // One claim per number. The bare header total is dropped while the note is
    // showing, so "500" is not also rendered on its own beside a note that
    // already carries and qualifies it.
    expect(screen.queryByText('500')).toBeNull()
  })

  it('leaves a complete listing unqualified', async () => {
    H.api.projectGitStatus.mockResolvedValue(capped(500, false))

    mount()

    expect(await screen.findByText('500 uncommitted')).toBeInTheDocument()
    // The bare header total is how a complete listing states its count, so it
    // must survive exactly when the note is absent.
    expect(screen.getByText('500')).toBeInTheDocument()
    expect(screen.queryByTestId('git-panel-truncated')).toBeNull()
    expect(screen.queryByText('500+ uncommitted')).toBeNull()
  })

  it('does not qualify a listing when the status read failed', async () => {
    // truncated cannot be trusted from a body the caller never received; a
    // rejected read must reach the error state, not a capped-list warning.
    H.api.projectGitStatus.mockRejectedValue(new Error('status unavailable'))

    mount()

    expect(await screen.findByTestId('git-panel-status-error')).toBeInTheDocument()
    expect(screen.queryByTestId('git-panel-truncated')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
  })
})


describe('GitPanel filter-driver refusal', () => {
  // `cause` is the refusal's discriminator: the backend knows whether config
  // DECLARED a driver or whether it could not be read, and says which.
  const refused = (code: string, message: string, cause = 'declared') =>
    Object.assign(new Error(message), {
      body: JSON.stringify({ error: message, code, cause }),
    })

  beforeEach(() => {
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'Status is off for this repository.'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'History is off for this repository.'),
    )
  })

  // The two routes fail on SEPARATE retry timers and notify React Query
  // separately, so a notice one route produced says nothing about the other:
  // the coalesced notice CAN render on one refusal alone (titled for that half
  // only, Refresh still live) before the other route fails. These wait for the
  // commit in which BOTH routes have failed.
  //
  // The scope-'both' title (gitFilterRefusalScope) is the one thing only that
  // commit renders, and the inert Refresh renders in the same commit.
  const findBothRefused = (titleKey = 'components.gitPanel.filter_refused_title') =>
    waitFor(() => {
      const notice = screen.getByTestId('git-panel-filter-refused')
      expect(notice).toHaveTextContent(i18next.t(titleKey))
      return notice
    })
  // The divergent branch: one notice per route, each from its own commit.
  const findBothNotices = () =>
    waitFor(() => ({
      statusNotice: screen.getByTestId('git-panel-status-error'),
      logNotice: screen.getByTestId('git-panel-log-error'),
    }))

  it('names the cause once instead of two generic failures', async () => {
    mount()

    const notice = await findBothRefused()
    expect(notice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused'))
    // A title, because a refusal and an outage both render through ErrorNotice:
    // without it the two conditions are the same box and a reader cannot tell a
    // standing policy from a failure.
    expect(notice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused_title'))
    // ONE notice: the cause is repo-level, so both routes refuse together and
    // two boxes would report one condition twice.
    expect(screen.queryByTestId('git-panel-status-error')).toBeNull()
    expect(screen.queryByTestId('git-panel-log-error')).toBeNull()
    // It is still an error surface with the agent hand-off (AUTOSDE
    // errors-use-error-notice): a filter-driver config is what the agent can
    // explain, so routing this around ErrorNotice would strip the one
    // affordance that helps.
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('makes refresh inert under a refusal and live under an outage', async () => {
    // The refusal copy promises retrying will not help, and a live refresh
    // control directly above that sentence contradicts it -- the re-fetch
    // returns the same 503 every time. An outage keeps it live, because there
    // a retry can genuinely succeed. Asserting BOTH is the point: disabling it
    // unconditionally would be a regression this test also catches.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'STATUS-SIDE-REFUSAL'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'),
    )
    mount()
    await findBothRefused()
    const inert = screen.getByRole('button', {
      name: i18next.t('components.gitPanel.refresh_unavailable'),
    })
    expect(inert).toBeDisabled()
    // The GLYPH has to change too. Dimming plus a hover tooltip was not enough
    // for a reader to tell the button was dead, so the slashed variant carries
    // the state in the shape. Asserting only `disabled` would let that revert
    // silently.
    expect(inert.querySelector('.lucide-refresh-cw-off')).not.toBeNull()
    expect(inert.querySelector('.lucide-refresh-cw')).toBeNull()
  })

  it('states the cause that fired, not both of them', async () => {
    // The guard refuses on two different facts: config NAMES a driver, or the
    // config probe could not prove one absent. Handing the reader both at once
    // made the common case -- an LFS repo, on every visit -- read a disjunction
    // the backend had already resolved.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )

    mount()

    const notice = await screen.findByTestId('git-panel-filter-refused')
    expect(notice).toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_unreadable'),
    )
    // And NOT the other cause's sentence, which would name a driver this
    // branch never saw.
    expect(notice).not.toHaveTextContent(i18next.t('components.gitPanel.filter_refused'))
  })

  it('keeps refresh live when the cause is an unreadable config', async () => {
    // Only a DECLARED driver is permanent. A config that could not be read can
    // become readable, and the status route re-polls every 5 s, so a retry here
    // can genuinely succeed -- deadening the control would be the same
    // over-claim this panel is being fixed for.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )

    mount()

    // Both facts together, so neither the pre-failure render nor an in-flight
    // window can satisfy this on its own -- and on the both-refused commit (the
    // scope-'both' title), because after the first refusal alone Refresh is
    // live whatever the cause.
    await waitFor(() => {
      const notice = screen.getByTestId('git-panel-filter-refused')
      expect(notice).toHaveTextContent(
        i18next.t('components.gitPanel.filter_refused_title_unreadable'),
      )
      expect(notice).toHaveTextContent(
        i18next.t('components.gitPanel.filter_refused_unreadable'),
      )
      const live = screen.getByRole('button', {
        name: i18next.t('components.gitPanel.refresh'),
      })
      expect(live).toBeEnabled()
    })
  })

  it('keeps refresh live once one route has recovered', async () => {
    // The refusal is permanent only while the repo config stands, and the
    // notice's own agent hand-off invites changing it in this window. The status
    // route then recovers on its 5 s interval while the log query -- no
    // interval, 30 s staleTime -- still holds its cached refusal. Keying the
    // inert state on the coalescing predicate disabled the one control that
    // could clear that, stranding the notice.
    H.api.projectGitStatus.mockResolvedValue({
      repo: true, branch: 'main', files: [], ahead: 0, behind: 0,
    })
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'),
    )

    mount()

    // Both facts in ONE waitFor, because they must hold AT THE SAME TIME. The
    // notice renders only while the coalescing predicate is true, so a run that
    // keyed the button on that predicate can never satisfy the pair -- whereas
    // asserting them in sequence passes on either the pre-failure render or the
    // in-flight refetch window, and proves nothing.
    await waitFor(() => {
      expect(screen.getByTestId('git-panel-filter-refused')).toBeInTheDocument()
      const live = screen.getByRole('button', {
        name: i18next.t('components.gitPanel.refresh'),
      })
      expect(live).toBeEnabled()
      expect(live.querySelector('.lucide-refresh-cw')).not.toBeNull()
    })
  })

  it('refetches the log when the status route stops refusing', async () => {
    // The automatic half of the same recovery: the branch-change effect cannot
    // fire on the FIRST successful status (it needs a previous marker, which a
    // refusing status never set), so without this the cached refusal outlives
    // the config change that cleared it.
    H.api.projectGitStatus.mockResolvedValue({
      repo: true, branch: 'main', files: [], ahead: 0, behind: 0,
    })
    // Two rejections, because the query's own `retry: 1` already spends a
    // second call on its own. Reaching a THIRD call is what proves an explicit
    // refetch happened rather than the built-in retry.
    H.api.projectGitLog
      .mockRejectedValueOnce(refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'))
      .mockRejectedValueOnce(refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'))
      .mockResolvedValue({
        repo: true,
        commits: [{
          sha: 'abc1234def', message: 'recovered', author: 'a',
          date: '2026-01-01T00:00:00Z', isHead: true,
        }],
      })

    mount()

    // A chain: the log read and its retry are refused, then GitPanel's recovery
    // effect (status readable, log still refusing) refetches the log.
    await waitFor(
      () => expect(H.api.projectGitLog.mock.calls.length).toBeGreaterThanOrEqual(3),
      LOG_REFETCHED,
    )
    // And the refusal is gone, so the panel is not left claiming history can't
    // be shown while the changes list renders beneath it. On the recovered
    // history, not merely without the notice: the refetch resets the log query's
    // error while it is in flight, so the in-flight frame has no notice either.
    await waitFor(() => {
      expect(screen.getByText('recovered')).toBeInTheDocument()
      expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    })
  })

  it('still refuses to claim the tree is clean', async () => {
    mount()

    await screen.findByTestId('git-panel-filter-refused')
    // The whole point of the change: a refused read is never a clean pill.
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText(/uncommitted/)).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })

  it('leaves a genuine outage in the error notice, which a retry can clear', async () => {
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_unavailable', "Couldn't read the repository status."),
    )
    H.api.projectGitLog.mockResolvedValue({ repo: true, commits: [] })

    mount()

    expect(await screen.findByTestId('git-panel-status-error')).toBeInTheDocument()
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    // Refresh stays LIVE here. "Which a retry can clear" is the name of this
    // test, so the control that performs the retry has to still work.
    expect(
      screen.getByRole('button', { name: i18next.t('components.gitPanel.refresh') }),
    ).toBeEnabled()
  })

  it('does not let the refusal suppress an independent status failure', async () => {
    // Two real, coexisting faults on one poll: a configured filter driver makes
    // the log route refuse, and a corrupt HEAD makes the status route fail for
    // its own reason. Coalescing into the refusal's single notice would hide the
    // status failure completely -- the same class of wrong answer as drawing a
    // clean pill over a status nobody read.
    recordError({ source: 'api', message: 'LOG-SIDE-REFUSAL', code: 'git_log_filter_refused' })
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_unavailable', 'STATUS-SIDE-OUTAGE'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'),
    )

    mount()

    // Each failure gets its own notice, and the coalesced one is NOT used.
    const { statusNotice, logNotice } = await findBothNotices()
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    // The refusal is titled and the outage is NOT, so the permanent and the
    // transient condition differ structurally. Two similar titles put the
    // difference back into body prose, which is what this asserts against.
    expect(statusNotice.querySelector('strong')).toBeNull()
    // The refusal names the half it is reporting, not both. The status notice
    // beside it already covers the changes half on its own terms, so a refusal
    // claiming "changes or history" here reports that sibling's failure as well
    // as its own -- and the reader's question was whether these were two
    // problems or one said twice.
    expect(logNotice).toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title_history'),
    )
    expect(logNotice).not.toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title'),
    )
    // Each notice's hand-off says which failure it carries. The reports differ
    // -- each has its own endpoint, status and code -- so with one shared label
    // the only indistinguishable part was the label.
    expect(
      within(statusNotice).getByRole('button', {
        name: i18next.t('components.gitPanel.ask_agent_changes'),
      }),
    ).toBeTruthy()
    expect(
      within(logNotice).getByRole('button', {
        name: i18next.t('components.gitPanel.ask_agent_history'),
      }),
    ).toBeTruthy()
    // The coded log refusal renders localized copy here too: the backend's
    // English sentence would be mixed-language copy in twelve catalogs.
    expect(logNotice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused'))
    expect(logNotice).not.toHaveTextContent('LOG-SIDE-REFUSAL')
    // Localizing the message breaks the hand-off's implicit lookup-by-message,
    // so the structured report must be passed explicitly or the agent receives
    // the sentence with no endpoint, status or code.
    await userEvent.click(within(logNotice).getByRole('button', { name: /ask the agent/i }))
    expect(consumeChatHandoff()).toContain('git_log_filter_refused')
  })

  it('localizes a coded status refusal in the divergent branch too', async () => {
    // Mirror of the log side. When the log route fails for its OWN reason the
    // panel stops coalescing, and the status refusal then rendered the
    // backend's English sentence under a localized title -- mixed-language copy
    // in the other twelve catalogs, the same defect already fixed one notice
    // over.
    recordError({ source: 'api', message: 'STATUS-SIDE-REFUSAL', code: 'git_status_filter_refused' })
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'STATUS-SIDE-REFUSAL'),
    )
    H.api.projectGitLog.mockRejectedValue(
      Object.assign(new Error('LOG-OUTAGE'), {
        body: JSON.stringify({ error: 'LOG-OUTAGE', code: 'git_log_unreadable' }),
      }),
    )

    mount()

    const statusNotice = await screen.findByTestId('git-panel-status-error')
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    // Mirror of the scope rule: here the refusal is the STATUS side, so it names
    // the changes half and leaves history to the log notice beside it.
    expect(statusNotice).toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title_changes'),
    )
    expect(statusNotice).not.toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title'),
    )
    expect(statusNotice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused'))
    expect(statusNotice).not.toHaveTextContent('STATUS-SIDE-REFUSAL')
    // The hand-off still carries the structured report, which a localized
    // message cannot resolve by lookup.
    await userEvent.click(within(statusNotice).getByRole('button', { name: /ask the agent/i }))
    expect(consumeChatHandoff()).toContain('git_status_filter_refused')
  })

  it('drops the stale-history clause when the log notice renders too', async () => {
    // Two boxes must not contradict each other. "Commit history may be out of
    // date" promises a list that is still on screen; when the log route failed
    // too there is no list, and the notice below says history can't be shown.
    recordError({ source: 'api', message: 'STATUS-OUTAGE', code: 'git_status_unavailable' })
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_unavailable', 'STATUS-OUTAGE'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'),
    )

    mount()

    const { statusNotice, logNotice } = await findBothNotices()
    expect(logNotice).toBeInTheDocument()
    expect(statusNotice).toHaveTextContent(
      i18next.t('components.gitPanel.status_failed_no_history'),
    )
    expect(statusNotice).not.toHaveTextContent(
      i18next.t('components.gitPanel.status_failed'),
    )
    // The notice now tells the reader to refresh, so the control it points at
    // has to be live here. It is, by construction: the button goes inert only
    // when BOTH routes refuse, and that is exactly the state where this notice
    // is replaced by the coalesced one. Pinned so the promise and the control
    // cannot drift apart.
    expect(statusNotice).toHaveTextContent(i18next.t('components.gitPanel.refresh'))
    expect(
      screen.getByRole('button', { name: i18next.t('components.gitPanel.refresh') }),
    ).toBeEnabled()
  })

  it('keeps the history clause when only the status route failed', async () => {
    // The other direction: with a commit list on screen, saying it may be stale
    // is the useful part, so suppressing it unconditionally is a regression.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_unavailable', 'STATUS-OUTAGE'),
    )
    H.api.projectGitLog.mockResolvedValue({ repo: true, commits: [] })

    mount()

    const statusNotice = await screen.findByTestId('git-panel-status-error')
    expect(statusNotice).toHaveTextContent(i18next.t('components.gitPanel.status_failed'))
  })

  it('names only the refusing half when the other route is healthy', async () => {
    // The coalesced notice is not always about both halves. Here the log route
    // succeeded, so the commit list is rendering underneath -- a refusal
    // claiming history cannot be shown denies something on screen.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'STATUS-SIDE-REFUSAL'),
    )
    H.api.projectGitLog.mockResolvedValue({ repo: true, commits: [] })

    mount()

    const notice = await screen.findByTestId('git-panel-filter-refused')
    expect(notice).toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title_changes'),
    )
    expect(notice).not.toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title'),
    )
  })

  it('puts the temporariness of an unreadable config on the headline', async () => {
    // The two causes take opposite advice and used to share one bold line, so a
    // reader who read only the headline could not tell the permanent condition
    // from the one that clears itself -- and then gives up on the wrong one.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'UNREADABLE-CONFIG', 'unreadable'),
    )

    mount()

    const notice = await findBothRefused('components.gitPanel.filter_refused_title_unreadable')
    expect(notice).toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title_unreadable'),
    )
    expect(notice).not.toHaveTextContent(
      i18next.t('components.gitPanel.filter_refused_title'),
    )
  })

  it('keeps the inert glyph legible at the size the header renders it', async () => {
    // The slash IS the difference between the two causes' controls: declared
    // deadens the button, unreadable leaves it live. At size 13 under this
    // repo's dominant opacity-40 the slash is the first thing to disappear, and
    // a reader shown three frames called all three crossed-out. A heavier stroke
    // and a smaller fade keep it on a still frame; the live button's resting
    // convention is untouched, so this is not a new treatment for one control.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'STATUS-SIDE-REFUSAL'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-SIDE-REFUSAL'),
    )

    mount()

    await findBothRefused()
    const inert = screen.getByRole('button', {
      name: i18next.t('components.gitPanel.refresh_unavailable'),
    })
    const glyph = inert.querySelector('.lucide-refresh-cw-off')
    expect(glyph).not.toBeNull()
    expect(glyph?.getAttribute('stroke-width')).toBe('2.5')
    expect(inert.className).toContain('opacity-60')
    expect(inert.className).not.toContain('opacity-40')
  })

  it('hands the agent a refusal report when it does coalesce', async () => {
    // Inside the coalesced branch every present error is a refusal, so the
    // report cannot carry a foreign code. Asserted on the hand-off PAYLOAD, not
    // rendered text: the report only reaches the agent prompt, so a DOM
    // assertion would pass whichever error was selected.
    recordError({ source: 'api', message: 'STATUS-REFUSAL', code: 'git_status_filter_refused' })
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'STATUS-REFUSAL'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'LOG-REFUSAL'),
    )

    mount()

    const notice = await screen.findByTestId('git-panel-filter-refused')
    await userEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    const handoff = consumeChatHandoff()
    expect(handoff).toContain('git_status_filter_refused')
    expect(handoff).not.toContain('git_status_unavailable')
  })
})

describe('GitPanel log route outage', () => {
  // The log route's discovery boundary now matches the status route: a failed
  // probe that is not Git's own not-a-repository verdict is a 503 with its own
  // code, not an empty commit list.
  const unavailable = (message: string) =>
    Object.assign(new Error(message), {
      body: JSON.stringify({ error: message, code: 'git_log_unavailable' }),
    })

  it('renders the coded log outage as an untitled localized notice, not an empty history', async () => {
    const serverMessage = 'server-only commit history detail'
    recordError({ source: 'api', message: serverMessage, code: 'git_log_unavailable' })
    H.api.projectGitLog.mockRejectedValue(unavailable(serverMessage))

    mount()

    const notice = await screen.findByTestId('git-panel-log-error')
    const localizedLog = i18next.t('components.gitPanel.log_failed')
    // Untitled, like the status outage: titled-vs-untitled is what separates a
    // transient outage from a permanent refusal at a glance.
    expect(notice.querySelector('strong')).toBeNull()
    expect(notice).toHaveTextContent(localizedLog)
    expect(notice).not.toHaveTextContent(serverMessage)
    expect(screen.getAllByText(localizedLog)).toHaveLength(1)
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    expect(screen.queryByTestId('git-panel-status-error')).toBeNull()
    // The changes half is healthy and stays so; the empty-history claim does
    // not, because no history was read.
    expect(screen.getByText('clean')).toBeInTheDocument()
    expect(screen.queryByText(i18next.t('components.gitPanel.empty_state'))).toBeNull()
    expect(screen.queryByText(i18next.t('components.gitPanel.commits'))).toBeNull()
    // A retry can clear an outage, so refresh stays live.
    expect(
      screen.getByRole('button', { name: i18next.t('components.gitPanel.refresh') }),
    ).toBeEnabled()
    // The hand-off carries the structured report the localized message cannot
    // resolve by lookup.
    await userEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    expect(consumeChatHandoff()).toContain('git_log_unavailable')
  })

  it('keeps an uncoded log failure titled with its own message', async () => {
    H.api.projectGitLog.mockRejectedValue(new Error('LOG-DETAIL'))

    mount()

    const notice = await screen.findByTestId('git-panel-log-error')
    expect(notice.querySelector('strong')).toHaveTextContent(
      i18next.t('components.gitPanel.log_failed'),
    )
    expect(notice).toHaveTextContent('LOG-DETAIL')
  })
})

describe('GitPanel request-error retry', () => {
  // The Refresh icon beside the header and the in-notice action read as two
  // different retries, so each request-error notice carries its own "Retry"
  // doing exactly what Refresh does: refetch that section's status AND log.
  const coded = (code: string, message: string) =>
    Object.assign(new Error(message), {
      body: JSON.stringify({ error: message, code }),
    })

  it('offers a Retry in the status error notice that refetches both reads and clears on success', async () => {
    // retry: 1 on the query spends a second attempt itself, so two rejections
    // reach the error state; the third attempt (the explicit Retry) succeeds.
    H.api.projectGitStatus
      .mockRejectedValueOnce(coded('git_status_unavailable', 'STATUS-OUTAGE'))
      .mockRejectedValueOnce(coded('git_status_unavailable', 'STATUS-OUTAGE'))
      .mockResolvedValue({ repo: true, repoRoot: PROJECT, branch: 'main', files: [] })

    mount()

    const notice = await screen.findByTestId('git-panel-status-error')
    const statusBefore = H.api.projectGitStatus.mock.calls.length
    const logBefore = H.api.projectGitLog.mock.calls.length
    await userEvent.click(within(notice).getByTestId('git-panel-status-retry'))

    // Does what Refresh does: both the status and the log route are refetched.
    await waitFor(() => {
      expect(H.api.projectGitStatus.mock.calls.length).toBeGreaterThan(statusBefore)
      expect(H.api.projectGitLog.mock.calls.length).toBeGreaterThan(logBefore)
    })
    // The now-successful status read clears the notice.
    await waitFor(() => expect(screen.queryByTestId('git-panel-status-error')).toBeNull())
  })

  it('offers a Retry in the log error notice that refetches both reads and clears on success', async () => {
    H.api.projectGitLog
      .mockRejectedValueOnce(coded('git_log_unavailable', 'LOG-OUTAGE'))
      .mockRejectedValueOnce(coded('git_log_unavailable', 'LOG-OUTAGE'))
      .mockResolvedValue({ repo: true, commits: [] })

    mount()

    const notice = await screen.findByTestId('git-panel-log-error')
    const statusBefore = H.api.projectGitStatus.mock.calls.length
    const logBefore = H.api.projectGitLog.mock.calls.length
    await userEvent.click(within(notice).getByTestId('git-panel-log-retry'))

    await waitFor(() => {
      expect(H.api.projectGitStatus.mock.calls.length).toBeGreaterThan(statusBefore)
      expect(H.api.projectGitLog.mock.calls.length).toBeGreaterThan(logBefore)
    })
    await waitFor(() => expect(screen.queryByTestId('git-panel-log-error')).toBeNull())
  })

  it('does not offer a Retry inside the filter-driver refusal notice', async () => {
    // The refusal copy says a retry will not help and Refresh is deliberately
    // inert, so the notice carries no Retry — only the hand-off.
    const refused = (code: string, message: string) =>
      Object.assign(new Error(message), {
        body: JSON.stringify({ error: message, code, cause: 'declared' }),
      })
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_filter_refused', 'Status is off for this repository.'),
    )
    H.api.projectGitLog.mockRejectedValue(
      refused('git_log_filter_refused', 'History is off for this repository.'),
    )

    mount()

    const notice = await waitFor(() => {
      const n = screen.getByTestId('git-panel-filter-refused')
      expect(n).toHaveTextContent(i18next.t('components.gitPanel.filter_refused_title'))
      return n
    })
    expect(
      within(notice).queryByText(i18next.t('components.gitPanel.repos_retry')),
    ).toBeNull()
    expect(screen.queryByTestId('git-panel-status-retry')).toBeNull()
    expect(screen.queryByTestId('git-panel-log-retry')).toBeNull()
  })
})

describe('GitPanel collapsed request failures', () => {
  it.each(['status', 'log', 'both'] as const)(
    'keeps %s Refresh failures and their hand-offs visible after the user collapses a clean section',
    async failedQuery => {
      mount()
      await screen.findByText('No changes or commits to display.')
      await waitFor(() => expect(H.api.projectGitLog).toHaveBeenCalledTimes(1))
      fireEvent.click(screen.getByTestId('git-repo-toggle'))
      expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'false')

      if (failedQuery !== 'log') {
        H.api.projectGitStatus.mockRejectedValue(new Error('STATUS-REFRESH-FAILED'))
      }
      if (failedQuery !== 'status') {
        const message = 'LOG-REFRESH-FAILED'
        H.api.projectGitLog.mockRejectedValue(Object.assign(new Error(message), {
          body: JSON.stringify({ error: message, code: 'git_log_unavailable' }),
        }))
        recordError({
          source: 'api', message, status: 503, code: 'git_log_unavailable',
          endpoint: '/api/project/git/log',
        })
      }
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

      if (failedQuery !== 'log') {
        const notice = await screen.findByTestId('git-panel-status-error')
        expect(notice).toBeVisible()
        expect(within(notice).getByRole('button', {
          name: i18next.t('components.gitPanel.ask_agent_changes'),
        })).toBeVisible()
      }
      if (failedQuery !== 'status') {
        const notice = await screen.findByTestId('git-panel-log-error')
        expect(notice).toBeVisible()
        await userEvent.click(within(notice).getByRole('button', {
          name: i18next.t('components.gitPanel.ask_agent_history'),
        }))
        expect(consumeChatHandoff()).toContain('- Request: /api/project/git/log -> HTTP 503')
      }
      expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'false')
    },
  )

  it('shows a history Refresh failure in a section that starts collapsed', async () => {
    mount(false)
    await screen.findByText('main')
    expect(H.api.projectGitLog).not.toHaveBeenCalled()
    H.api.projectGitLog.mockRejectedValue(new Error('LOG-REFRESH-FAILED'))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByTestId('git-panel-log-error')).toBeVisible()
    expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'false')
  })

  it('says in words that a collapsed heading opens, and drops the hint once open', async () => {
    mount(false)
    await screen.findByText('main')
    const toggle = screen.getByTestId('git-repo-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // The collapsed toggle's native tooltip names the expand action; it draws
    // over the page, so it never shifts a row the way an inline reveal did.
    expect(toggle).toHaveAttribute('title', 'Show changes and history')
    // Inside the button, so the toggle's accessible name says what it opens.
    const drawnHint = within(toggle).getByTestId('git-repo-expand-hint')
    expect(drawnHint).toHaveTextContent('Show changes and history')
    // Drawn plainly on the row that carries the resting hint: visible, with no
    // sr-only and no hover/focus gating.
    expect(drawnHint.className).not.toMatch(/\bsr-only\b/)
    expect(drawnHint.className).not.toContain('group-hover:not-sr-only')
    expect(drawnHint.className).not.toContain('group-focus-visible:not-sr-only')
    expect(screen.getByRole('button', { name: /Show changes and history/ })).toBe(toggle)

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByTestId('git-repo-expand-hint')).toBeNull()
    // Open now, so the tooltip names the collapse action instead.
    expect(toggle).toHaveAttribute('title', 'Hide changes and history')
  })

  it('keeps a hidden hint in the accessible name, with no hover reveal, when the list draws it on another row', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    render(
      <QueryClientProvider client={queryClient}>
        <GitPanel
          projectDir={PROJECT}
          section={{ label: PROJECT, defaultOpen: false, expandHintVisible: false }}
        />
      </QueryClientProvider>,
    )
    await screen.findByText('main')
    const toggle = screen.getByTestId('git-repo-toggle')
    const hint = within(toggle).getByTestId('git-repo-expand-hint')
    // Visually hidden rather than dropped: a screen reader reads rows one at
    // a time and gets no "same as the row above" from alignment, so each
    // collapsed toggle still has to say what it opens.
    expect(hint.className).toMatch(/\bsr-only\b/)
    // Hidden and kept hidden: no hover/focus reveal, because a reveal that
    // flipped sr-only off pushed every row below it down. The toggle is not a
    // Tailwind `group`, and the hint carries no group-hover/-focus utilities.
    expect(toggle).not.toHaveClass('group')
    expect(hint.className).not.toContain('group-hover:not-sr-only')
    expect(hint.className).not.toContain('group-focus-visible:not-sr-only')
    // The native title carries the hint instead, so a pointer user still gets it.
    expect(toggle).toHaveAttribute('title', 'Show changes and history')
    expect(toggle).toHaveAccessibleName(/Show changes and history/)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // Opening drops the hint from this row too.
    fireEvent.click(toggle)
    expect(screen.queryByTestId('git-repo-expand-hint')).toBeNull()
  })
})

describe('GitPanel section heading', () => {
  it('toggles from the branch and change-count line, with Refresh outside the toggle', async () => {
    mount(false)
    await screen.findByText('main')
    const toggle = screen.getByTestId('git-repo-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    // The heading's two lines look like one block, so both lines are the one
    // toggle; Refresh is the only other control and sits beside it, never
    // nested (a button inside a button is invalid and would toggle on press).
    const refresh = screen.getByRole('button', { name: 'Refresh' })
    expect(toggle).not.toContainElement(refresh)
    expect(within(toggle).queryByRole('button')).toBeNull()
    fireEvent.click(refresh)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(screen.getByText('main'))
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByText('clean'))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // The toggle still names the repository and, closed, says that it opens.
    expect(screen.getByRole('button', { name: /project.*Show changes and history/ })).toBe(toggle)
  })

  it('places the collapsed hint right after the path, not at the far edge', async () => {
    mount(false)
    await screen.findByText('main')
    const hint = screen.getByTestId('git-repo-expand-hint')
    // Same line as the path, immediately after it (no badge on this section),
    // and not pushed away from it: a hint at the far right read as a stray
    // caption rather than part of the toggle.
    expect(hint.previousElementSibling).toHaveTextContent('project')
    expect(hint.className).not.toMatch(/\bml-auto\b/)
    expect(hint.parentElement).not.toContainElement(screen.getByText('main'))
  })

  it('lets a long repository name truncate and the hint wrap, so nothing overlaps at 320px', async () => {
    mount(false)
    await screen.findByText('main')
    // The basename used to be `shrink-0` with no truncation: at 320px a
    // worktree named for its branch ran under the hint beside it. jsdom lays
    // nothing out, so the pin is the classes that decide the layout.
    const name = screen.getByTestId('git-repo-name')
    expect(name).toHaveTextContent('project')
    expect(name.className).toMatch(/\bmin-w-0\b/)
    expect(name.className).toMatch(/\btruncate\b/)
    expect(name.className).not.toMatch(/\bshrink-0\b/)
    // The full path stays reachable on the heading itself; the toggle's own
    // title names the expand/collapse action, which draws over nothing.
    expect(screen.getByTestId('git-repo-heading')).toHaveAttribute('title', PROJECT)
    expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('title', 'Show changes and history')
    // The hint may give way: neither pinned to its width nor forced onto one
    // line, and its row wraps, so when the two do not fit the hint drops under
    // the path instead of overlapping it.
    const hint = screen.getByTestId('git-repo-expand-hint')
    expect(hint.className).not.toMatch(/\bshrink-0\b/)
    expect(hint.className).not.toMatch(/\b(whitespace-nowrap|truncate|text-nowrap)\b/)
    expect(hint.parentElement?.className).toMatch(/\bflex-wrap\b/)
    // The chevron, path and badge wrap as ONE item: a long path must not
    // leave the chevron alone on the line above it.
    const heading = screen.getByTestId('git-repo-heading')
    expect(heading.parentElement).toBe(hint.parentElement)
    expect(heading).toContainElement(name)
    expect(heading.querySelector('svg')).not.toBeNull()
    expect(heading.className).toMatch(/\bmin-w-0\b/)
  })
})

describe('GitPanel list-section status polling', () => {
  /** One status poll interval plus slack, so a timer that fires would have fired. */
  const POLL_WINDOW_MS = 5000 * 2 + 500

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('fetches a collapsed section once and does not poll it on the interval', async () => {
    const { unmount } = mount(false)
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledTimes(1))
    // The header still shows what the one fetch returned.
    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'false')

    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })

    // Up to MAX_SLOT_GIT_REPOS sections each run several git subprocesses per
    // poll; a collapsed one must not add to that.
    expect(H.api.projectGitStatus).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('starts polling when the user opens the section and stops when they close it', async () => {
    const { unmount } = mount(false)
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('git-repo-toggle'))
    expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'true')
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })
    await waitFor(() => expect(H.api.projectGitStatus.mock.calls.length).toBeGreaterThan(1))

    fireEvent.click(screen.getByTestId('git-repo-toggle'))
    expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'false')
    // Let any poll already in flight settle before counting.
    await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
    const afterClose = H.api.projectGitStatus.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })
    expect(H.api.projectGitStatus).toHaveBeenCalledTimes(afterClose)
    unmount()
  })

  it('keeps polling a section that opened itself', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      repoRoot: PROJECT,
      branch: 'main',
      files: [{ path: 'a.txt', status: 'M', staged: false }],
    })
    const dirty = mount(false)
    await waitFor(() => expect(screen.getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'true'))
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })
    await waitFor(() => expect(H.api.projectGitStatus.mock.calls.length).toBeGreaterThan(1))
    dirty.unmount()
  })
})
