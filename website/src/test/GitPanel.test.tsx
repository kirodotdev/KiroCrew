import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

const PROJECT = '/workspace/project'

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GitPanel projectDir={PROJECT} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

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
    // statusError, so wait for that bounded request chain rather than the
    // default one-second query timeout.
    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
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

    const notice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
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

    const notice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    expect(notice).toHaveTextContent('无法读取仓库状态。提交历史可能已过时。')
    expect(notice).not.toHaveTextContent(serverMessage)
    await userEvent.click(within(notice).getByRole('button', { name: '询问代理' }))

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

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
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

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('Changes')).toBeNull()
    expect(screen.queryByTitle('src/a.ts')).toBeNull()
  })

  it('keeps the clean-repository label, badge, and empty state', async () => {
    mount()

    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText('clean')).toBeInTheDocument()
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
    expect(screen.getByText('1 uncommitted')).toBeInTheDocument()
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

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
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

  it('names the cause once instead of two generic failures', async () => {
    mount()

    const notice = await screen.findByTestId('git-panel-filter-refused', {}, { timeout: 5000 })
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
    await screen.findByTestId('git-panel-filter-refused', {}, { timeout: 5000 })
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

    const notice = await screen.findByTestId('git-panel-filter-refused', {}, { timeout: 5000 })
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
    // window can satisfy this on its own.
    await waitFor(() => {
      const notice = screen.getByTestId('git-panel-filter-refused')
      expect(notice).toHaveTextContent(
        i18next.t('components.gitPanel.filter_refused_unreadable'),
      )
      const live = screen.getByRole('button', {
        name: i18next.t('components.gitPanel.refresh'),
      })
      expect(live).toBeEnabled()
    }, { timeout: 5000 })
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
    }, { timeout: 5000 })
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
      .mockResolvedValue({ repo: true, commits: [] })

    mount()

    await waitFor(
      () => expect(H.api.projectGitLog.mock.calls.length).toBeGreaterThanOrEqual(3),
      { timeout: 5000 },
    )
    // And the refusal is gone, so the panel is not left claiming history can't
    // be shown while the changes list renders beneath it.
    await waitFor(
      () => expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull(),
      { timeout: 5000 },
    )
  })

  it('still refuses to claim the tree is clean', async () => {
    mount()

    await screen.findByTestId('git-panel-filter-refused', {}, { timeout: 5000 })
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

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
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
    const statusNotice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    const logNotice = screen.getByTestId('git-panel-log-error')
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    // The refusal is titled and the outage is NOT, so the permanent and the
    // transient condition differ structurally. Two similar titles put the
    // difference back into body prose, which is what this asserts against.
    expect(statusNotice.querySelector('strong')).toBeNull()
    expect(logNotice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused_title'))
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

    const statusNotice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    expect(screen.queryByTestId('git-panel-filter-refused')).toBeNull()
    expect(statusNotice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused_title'))
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

    const statusNotice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    expect(screen.getByTestId('git-panel-log-error')).toBeInTheDocument()
    expect(statusNotice).toHaveTextContent(
      i18next.t('components.gitPanel.status_failed_no_history'),
    )
    expect(statusNotice).not.toHaveTextContent(
      i18next.t('components.gitPanel.status_failed'),
    )
  })

  it('keeps the history clause when only the status route failed', async () => {
    // The other direction: with a commit list on screen, saying it may be stale
    // is the useful part, so suppressing it unconditionally is a regression.
    H.api.projectGitStatus.mockRejectedValue(
      refused('git_status_unavailable', 'STATUS-OUTAGE'),
    )
    H.api.projectGitLog.mockResolvedValue({ repo: true, commits: [] })

    mount()

    const statusNotice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    expect(statusNotice).toHaveTextContent(i18next.t('components.gitPanel.status_failed'))
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

    const notice = await screen.findByTestId('git-panel-filter-refused', {}, { timeout: 5000 })
    await userEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    const handoff = consumeChatHandoff()
    expect(handoff).toContain('git_status_filter_refused')
    expect(handoff).not.toContain('git_status_unavailable')
  })
})
