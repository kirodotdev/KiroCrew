/**
 * The Session Manager plugin row when the plugin is installed but refused by the
 * gateway's safety check: the reason is said in the reader's language from the
 * kind of refusal the gateway names, the diagnosis is the alert, and the steps
 * and the ownership command's note sit under it as plain text.
 */
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import {
  REMOTE_CREW_PREFLIGHT_GUIDE_URL,
  awsCliRefusalCopy,
  awsCliRefusalReport,
  credentialHelperMissingSentence,
  pluginRefusalCopy,
  pluginRefusalReport,
} from '../pages/settings/remoteCrewPluginRefusal'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import type { CloudPreflight } from '../api/client'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    isAuthExpiredError: () => false,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})
import { api } from '../api/client'

const PLUGIN = '/usr/local/sessionmanagerplugin/bin/session-manager-plugin'
const CHOWN = 'sudo chown -R root:wheel /usr/local/sessionmanagerplugin'
const PREFLIGHT_OK: CloudPreflight = {
  reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev',
  ec2_reachable: true, cloudformation_reachable: true, ssm_reachable: true,
  session_manager_plugin: true, note: '', detail: '',
}
const AWS_EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'signin', label: 'Sign in to Kiro' },
    { key: 'connect', label: 'Connect' },
  ],
}

/** A preflight whose plugin was found at PLUGIN and refused. */
function refused(over: Partial<CloudPreflight>): CloudPreflight {
  return {
    ...PREFLIGHT_OK,
    session_manager_plugin: false,
    session_manager_plugin_command: '',
    session_manager_plugin_refused_path: PLUGIN,
    session_manager_plugin_refused_reason: '',
    session_manager_plugin_refused_code: '',
    session_manager_plugin_refused_owner: '',
    session_manager_plugin_refused_owner_uid: null,
    ...over,
  }
}

async function openSetup(preflight: CloudPreflight) {
  vi.mocked(api.cloudPreflight).mockResolvedValue(preflight)
  const u = userEvent.setup()
  renderWithProviders(<RemoteCrewPanel />)
  await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
  const notice = (await screen.findByText(/^Found at \/usr\/local\/sessionmanagerplugin/)).closest('[role="alert"]') as HTMLElement
  expect(notice).not.toBeNull()
  return { notice, row: notice.closest('li') as HTMLElement }
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } })
})

describe('RemoteCrewPanel refused plugin row', () => {
  it('says the refusal in its own words, and sets the steps and the command note outside the alert', async () => {
    const reason = 'executable parent is owned by another user (uid 1501)'
    const { notice, row } = await openSetup(refused({
      session_manager_plugin_command: CHOWN,
      session_manager_plugin_refused_reason: reason,
      session_manager_plugin_refused_code: 'other_owner',
      session_manager_plugin_refused_owner_uid: 1501,
    }))

    // The diagnosis: translated from the code, not the check's English words.
    expect(notice).toHaveTextContent(/it, or a folder above it, belongs to another user account \(user ID 1501, which has no name on the computer that runs .+\)\. .+ runs only programs owned by its own account or by the system administrator account \(root\)\./)
    // The check's own words are the notice's smaller monospace second line,
    // and the hand-off carries them (see the report test below).
    const raw = within(notice).getByText(reason)
    expect(raw.className).toMatch(/\bfont-mono\b/)
    expect(within(notice).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    // The reinstall caution and the steps are plain text, not inside the alert.
    const steps = within(row).getByText(/reinstall it from AWS first\. Then run the command below to make its install folder root-owned/)
    expect(notice.contains(steps)).toBe(false)
    expect(notice).not.toHaveTextContent(/reinstall/)
    const install = within(row).getByRole('link', { name: /Open AWS’s Session Manager plugin install page/ })
    expect(notice.contains(install)).toBe(false)
    expect(install).toHaveAttribute(
      'href',
      'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html',
    )
    // The caution comes before the command; the note on where it runs follows it.
    const command = within(row).getByText(CHOWN)
    const note = within(row).getByText(/Run it in a terminal on the computer that runs .+, from an administrator account: sudo asks for that account’s password\. It changes only who owns the plugin’s install folder and the files in it\./)
    expect(steps.compareDocumentPosition(command) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(command.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(notice.contains(note)).toBe(false)
    expect(screen.queryByText(/Not installed/)).not.toBeInTheDocument()
    // The row's warning circle is the only triangle.
    expect(row.querySelectorAll('.lucide-triangle-alert')).toHaveLength(1)
  })

  it('names the owning account when the gateway knows its name', async () => {
    const { notice } = await openSetup(refused({
      session_manager_plugin_command: CHOWN,
      session_manager_plugin_refused_reason: 'executable is owned by another user (uid 502)',
      session_manager_plugin_refused_code: 'other_owner',
      session_manager_plugin_refused_owner: 'alex',
      session_manager_plugin_refused_owner_uid: 502,
    }))
    expect(notice).toHaveTextContent(/it, or a folder above it, belongs to another user account \(alex, user ID 502\)\./)
  })

  it('falls back to the check’s English reason for a kind it has no words for', async () => {
    const reason = 'executable hierarchy is not accessible'
    const { notice, row } = await openSetup(refused({ session_manager_plugin_refused_reason: reason }))
    expect(notice).toHaveTextContent(`will not run it: ${reason}.`)
    // Shown once: no separate detail line repeats it.
    expect(within(row).getAllByText(new RegExp(reason))).toHaveLength(1)
    // No known command: the next step is plain text, with no command or note,
    // and it points at the hand-off for a reader who does not know how.
    const next = within(row).getByText('Fix the file’s ownership or permissions, then re-check. If you are not sure how, use “Ask the agent”.')
    expect(notice.contains(next)).toBe(false)
    expect(within(row).queryByRole('button', { name: /Copy command/i })).toBeNull()
    expect(within(row).queryByText(/Run it in a terminal/)).toBeNull()
    expect(within(row).queryByRole('link', { name: /install page/ })).toBeNull()
    // With no command to offer, the row points at the guide's troubleshooting entry.
    const guide = within(row).getByRole('link', { name: 'Open the troubleshooting steps in the Remote Crew on EC2 guide' })
    expect(notice.contains(guide)).toBe(false)
    expect(guide).toHaveAttribute('href', REMOTE_CREW_PREFLIGHT_GUIDE_URL)
    expect(REMOTE_CREW_PREFLIGHT_GUIDE_URL).toMatch(/docs\/guides\/remote-crew-on-ec2\.md#set-up-pre-flight-credential-helper-not-found-or-plugin-refused$/)
  })

  it('offers no guide link when it shows the ownership command', async () => {
    const { row } = await openSetup(refused({
      session_manager_plugin_command: CHOWN,
      session_manager_plugin_refused_reason: 'executable is owned by another user (uid 502)',
      session_manager_plugin_refused_code: 'other_owner',
      session_manager_plugin_refused_owner_uid: 502,
    }))
    expect(within(row).queryByRole('link', { name: /Remote Crew on EC2 guide/ })).toBeNull()
  })
})

describe('credentialHelperMissingSentence', () => {
  it('leads with the edit, names the config file and the setting, and says how to find the path', () => {
    const s = credentialHelperMissingSentence('sso-cred-helper')
    // `which` only finds the helper on the gateway's computer, as the gateway's
    // own account, so the sentence says where to run it.
    expect(s).toMatch(new RegExp(
      '^Give sso-cred-helper its full path in the credential_process line of your AWS config file \\(usually ~/\\.aws/config\\), then re-check\\. '
      + 'The AWS CLI could not find it on the gateway’s PATH\\. To get the full path, run which sso-cred-helper in a terminal '
      + 'on the computer that runs \\S.*, from the account that runs it\\.$',
    ))
    expect(s).not.toMatch(/It is usually the program/)
  })
})

describe('pluginRefusalCopy', () => {
  const base = { session_manager_plugin_refused_reason: 'the check’s words' }

  it.each([
    ['world_writable', /will not run it: any account can change the file or a folder above it\.$/],
    ['root_gateway', /will not run it: .+ is running as root, and it runs no helper program as root\.$/],
    ['strict_policy', /will not run it: the strict check \(KIROCREW_PROVIDER_BIN_STRICT\) is on, and it accepts only a file and folders that only root can change, at a path with no symbolic links\.$/],
  ])('says the %s refusal in its own sentence', (code, sentence) => {
    const copy = pluginRefusalCopy(refused({ ...base, session_manager_plugin_refused_code: code }))
    expect(copy?.failure).toMatch(sentence)
    expect(copy?.failure).toMatch(/^Found at \/usr\/local\/sessionmanagerplugin\/bin\/session-manager-plugin, /)
    expect(copy?.failureDetail).toBe('the check’s words')
  })

  it.each([
    ['root_gateway', /^Start .+ from a regular account instead of root, then re-check\.$/],
    ['strict_policy', /^Install it where only root can change the file and its folders, at a path with no symbolic links, then re-check\./],
    ['world_writable', /^Fix the file’s ownership or permissions, then re-check\./],
    ['other_owner', /^Install a copy with AWS’s own installer, then re-check\. .+ runs that copy only if it and every folder above it belong to root or to the account that runs .+\. If a folder such as \/usr\/local\/bin belongs to the other account, an administrator must make root its owner first\. The guide shows how\.$/],
    ['', /^Fix the file’s ownership or permissions, then re-check\./],
  ])('gives the %s refusal a next step that can clear it', (code, step) => {
    const copy = pluginRefusalCopy(refused({ ...base, session_manager_plugin_refused_code: code, session_manager_plugin_refused_owner_uid: 7 }))
    expect(copy?.steps.text).toMatch(step)
  })

  it('uses the English reason for an owner it can neither name nor number', () => {
    const copy = pluginRefusalCopy(refused({ ...base, session_manager_plugin_refused_code: 'other_owner' }))
    expect(copy?.failure).toMatch(/will not run it: the check’s words\.$/)
    expect(copy?.failureDetail).toBeUndefined()
  })

  it('hands the agent the code and the check’s words with the sentence', () => {
    const p = refused({ ...base, session_manager_plugin_refused_code: 'world_writable' })
    const copy = pluginRefusalCopy(p)!
    const report = pluginRefusalReport(p, copy)
    expect(report).toMatchObject({
      source: 'system',
      code: 'world_writable',
      message: copy.failure,
      detail: 'the check’s words',
      endpoint: '/api/cloud/preflight',
    })
  })

  it('has nothing to say for an accepted or a missing plugin', () => {
    expect(pluginRefusalCopy(PREFLIGHT_OK)).toBeNull()
    expect(pluginRefusalCopy(refused({ session_manager_plugin_refused_path: '' }))).toBeNull()
  })

  it('adds the command note only when there is a command', () => {
    expect(pluginRefusalCopy(refused(base))?.commandNote).toBeUndefined()
    expect(pluginRefusalCopy(refused({ ...base, session_manager_plugin_command: CHOWN }))?.commandNote).toMatch(/^Run it in a terminal/)
  })
})

describe('RemoteCrewPanel refused AWS CLI', () => {
  const CLI = '/opt/homebrew/bin/aws'
  const cliRefused = (over: Partial<CloudPreflight> = {}): CloudPreflight => ({
    ...PREFLIGHT_OK,
    reachable: false,
    account: '',
    detail: 'aws CLI not found — install it first (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)',
    credential_helper_missing: 'aws',
    aws_cli_refused_path: CLI,
    aws_cli_refused_reason: 'executable is owned by another user (uid 502)',
    aws_cli_refused_code: 'other_owner',
    aws_cli_refused_owner: 'workbrew',
    aws_cli_refused_owner_uid: 502,
    ...over,
  })

  it('says where the CLI was found and why, not that it is missing', async () => {
    vi.mocked(api.cloudPreflight).mockResolvedValue(cliRefused())
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: /Set up a new one/i }))
    const notice = (await screen.findByText(/^Your AWS credentials were not checked\. Found at \/opt\/homebrew\/bin\/aws, /)).closest('[role="alert"]') as HTMLElement
    expect(notice).toHaveTextContent(/belongs to another user account \(workbrew, user ID 502\)/)
    expect(within(notice).getByText('executable is owned by another user (uid 502)')).toBeInTheDocument()
    const row = notice.closest('li') as HTMLElement
    // No install link, no helper advice and no command: none clears this refusal.
    expect(row).not.toHaveTextContent(/not found|install it first|credential_process/)
    expect(within(row).queryByRole('button', { name: /Copy command/i })).toBeNull()
    // Not the ownership step: making another account's file trusted is the risk.
    const next = within(row).getByText(/^Install a copy with AWS’s own installer, then re-check\./)
    expect(row).not.toHaveTextContent(/Fix the file’s ownership or permissions/)
    expect(next).toHaveTextContent(/If a folder such as \/usr\/local\/bin belongs to the other account/)
    expect(notice.contains(next)).toBe(false)
    expect(within(row).getByRole('link', { name: 'Open the troubleshooting steps in the Remote Crew on EC2 guide' }))
      .toHaveAttribute('href', REMOTE_CREW_PREFLIGHT_GUIDE_URL)
  })

  it('gives a root gateway the step that clears it', () => {
    const copy = awsCliRefusalCopy(cliRefused({ aws_cli_refused_code: 'root_gateway' }))
    expect(copy?.failure).toMatch(/is running as root, and it runs no helper program as root\.$/)
    expect(copy?.steps.text).toMatch(/^Start .+ from a regular account instead of root, then re-check\.$/)
    expect(copy?.commandNote).toBeUndefined()
  })

  it('gives a world-writable CLI the ownership step', () => {
    const copy = awsCliRefusalCopy(cliRefused({ aws_cli_refused_code: 'world_writable' }))
    expect(copy?.steps.text).toMatch(/^Fix the file’s ownership or permissions, then re-check\./)
  })

  it('hands the agent its own report id, code and the check’s words', () => {
    const p = cliRefused()
    const copy = awsCliRefusalCopy(p)!
    expect(awsCliRefusalReport(p, copy)).toMatchObject({
      id: 'remote-crew-aws-cli-refused',
      code: 'other_owner',
      message: copy.failure,
      detail: 'executable is owned by another user (uid 502)',
    })
  })

  it('has nothing to say once credentials resolve or when no CLI was refused', () => {
    expect(awsCliRefusalCopy(cliRefused({ reachable: true, account: '1234' }))).toBeNull()
    expect(awsCliRefusalCopy(cliRefused({ aws_cli_refused_path: '' }))).toBeNull()
  })
})
