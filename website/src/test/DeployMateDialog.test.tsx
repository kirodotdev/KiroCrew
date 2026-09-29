/**
 * The mate picker's two steps, driven as the user drives them.
 *
 * `remoteCrewsAndMates.test.ts` covers `laneChoices` as a pure function; this covers
 * what the dialog RENDERS, which is where consent lives: a launch is money, and the only
 * thing between a chosen mate and a started task is one checkbox.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import DeployMateDialog from '../pages/settings/DeployMateDialog'

vi.mock('../api/client', () => ({
  api: {
    members: vi.fn(),
    cloudProvisioners: vi.fn(),
  },
}))
import { api } from '../api/client'

const FARGATE_ROW = {
  id: 'aws_fargate',
  kind: 'aws_fargate',
  label: 'AWS Fargate in your own account',
  posix_only: false,
  serves_mate: 'demo',
  confirm_before_launch: 'kirocrew/crew/demo/KIRO_IDENTITY',
  steps: [{ key: 'provision', label: 'Run the task' }],
}
const EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [{ key: 'provision', label: 'Create the instance' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.members).mockResolvedValue({
    members: [
      { name: 'demo', slug: 'demo', display_name: '', avatar: '' },
      { name: 'orchard-sde', slug: 'orchard-sde', display_name: '', avatar: '' },
    ],
  } as never)
  vi.mocked(api.cloudProvisioners).mockResolvedValue({
    provisioners: [EC2_ROW, FARGATE_ROW],
  } as never)
})

/** Open the dialog, pick `name`, and reach the confirmation. */
async function reachConfirm(u: ReturnType<typeof userEvent.setup>, name: string) {
  await u.click(await screen.findByRole('option', { name, exact: true }))
  await u.click(screen.getByTestId('deploy-mate-continue'))
  return screen.findByTestId('deploy-mate-confirm')
}

describe('DeployMateDialog', () => {
  it('asks for a mate, never for a crew, and offers no EC2 lane', async () => {
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    expect(await screen.findByText('Deploy a mate to the cloud')).toBeInTheDocument()
    expect(screen.getByText('Which mate?')).toBeInTheDocument()
    // The roster count is the answer to "is the mate I want in here at all".
    expect(await screen.findByText('2 of 2 mates')).toBeInTheDocument()
    // EC2 installs a gateway, so it is not one of the places a mate can run.
    expect(await screen.findByRole('button', { name: /Fargate/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /EC2/ })).not.toBeInTheDocument()
  })

  it('starts the confirmation UNCHECKED and keeps Launch off until it is ticked', async () => {
    // The gate is the tick. A box that arrived ticked made Launch's dependency on it
    // decoration -- the reader had to UNDO it to say no, which is not how consent reads.
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    await reachConfirm(u, 'demo')

    const box = screen.getByRole('checkbox')
    expect(box).not.toBeChecked()
    expect(screen.getByTestId('deploy-mate-launch')).toBeDisabled()

    await u.click(box)

    expect(box).toBeChecked()
    expect(screen.getByTestId('deploy-mate-launch')).toBeEnabled()
  })

  it('says the sentence ONCE, as the checkbox, and names the region in it', async () => {
    // The sentence used to be printed above the box AND as its label, word for word,
    // which left the box nothing of its own to say. What sits above it now is the one
    // thing the sentence does not carry: that nothing exists yet.
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    await reachConfirm(u, 'demo')

    // The kind word "mate" sits beside the operand, which is what lets the name carry a
    // space without blurring into the rest of the sentence (destructiveConfirm #4657).
    const sentence = 'Launch the mate demo on Fargate in us-west-2'
    expect(screen.getAllByText(sentence)).toHaveLength(1)
    expect(screen.getByRole('checkbox', { name: sentence })).toBeInTheDocument()
    expect(screen.getByText('Nothing is created until you press Launch.')).toBeInTheDocument()
  })

  it('sends the lane, the mate and the recipient verbatim once confirmed', async () => {
    const onLaunch = vi.fn()
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={onLaunch} launching={false} region="us-west-2" />,
    )

    await reachConfirm(u, 'demo')
    await u.click(screen.getByRole('checkbox'))
    await u.click(screen.getByTestId('deploy-mate-launch'))

    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1))
    expect(onLaunch.mock.calls[0][0]).toMatchObject({
      mateName: 'demo',
      confirmRecipient: 'kirocrew/crew/demo/KIRO_IDENTITY',
    })
    expect(onLaunch.mock.calls[0][0].provisioner.id).toBe('aws_fargate')
  })

  it('sends the roster IDENTIFIER, never the label shown beside it', async () => {
    // `serves_mate` and the engine's own `mate_name_refusal` compare byte-for-byte
    // against the name the image's secret binds, which is the roster's `name`. A row
    // whose `display_name` differs reads as that label, so sending what the reader SAW
    // would be refused at the boundary for a mate the lane actually serves.
    vi.mocked(api.members).mockResolvedValue({
      members: [{ name: 'demo', slug: 'demo', display_name: 'Demo Analyst', avatar: '' }],
    } as never)
    const onLaunch = vi.fn()
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={onLaunch} launching={false} region="us-west-2" />,
    )

    // The label is what the reader clicks.
    await u.click(await screen.findByRole('option', { name: 'Demo Analyst', exact: true }))
    // And the lane accepts it, which it only can if the IDENTIFIER was what matched.
    expect(await screen.findByText('Serves demo')).toBeInTheDocument()
    expect(screen.queryByText(/cannot deploy/i)).not.toBeInTheDocument()

    await u.click(screen.getByTestId('deploy-mate-continue'))
    await u.click(await screen.findByRole('checkbox'))
    await u.click(screen.getByTestId('deploy-mate-launch'))

    await waitFor(() => expect(onLaunch).toHaveBeenCalled())
    expect(onLaunch.mock.calls[0][0].mateName).toBe('demo')
  })

  it('says why an unusable lane is unusable WITHOUT it being selected first', async () => {
    // A chip reading "Not set up" is one a reader will not click, so a reason reachable
    // only by selecting it is a reason its own audience never sees. A Fargate lane with
    // no cluster configured is that case: "why can I not deploy to Fargate" has to be
    // answerable on sight.
    vi.mocked(api.cloudProvisioners).mockResolvedValue({
      provisioners: [EC2_ROW],
    } as never)
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    // Wait for the LANES, not just the dialog: before that answer arrives every chip
    // reads as absent, which would make the assertion below pass for the wrong reason.
    expect(await screen.findByRole('button', { name: /Fargate/ })).toBeInTheDocument()
    expect(screen.getByText(/Fargate is not set up on this gateway/i)).toBeInTheDocument()
  })

  it('names what the launch costs before the button that starts the spending', async () => {
    // A Fargate task bills for as long as it runs. With no cost line anywhere in the
    // flow, the only cost information in front of Launch was the absence of any -- and a
    // reader who cannot tell what a click costs does not click. Its own sentence, not the
    // EC2 launcher's: a task has no instance, no disk and no setup bucket.
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    await reachConfirm(u, 'demo')

    expect(screen.getByText(/billed to you: one Fargate task/i)).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /Open the AWS Pricing Calculator/i }),
    ).toHaveAttribute('href', 'https://calculator.aws')
  })

  it('returns to the picker from the confirmation, and labels that Back', async () => {
    // The button runs setStep('pick'). Labelled Cancel it promised to leave a flow it
    // puts the reader back into.
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    await reachConfirm(u, 'demo')
    await u.click(screen.getByRole('button', { name: 'Back' }))

    expect(await screen.findByTestId('deploy-mate-picker')).toBeInTheDocument()
  })

  it('refuses to continue for a mate this lane cannot serve, and says which it serves', async () => {
    // The chip is selectable rather than `disabled` so its reason can be reached at all;
    // Continue is what refuses.
    const u = userEvent.setup()
    renderWithProviders(
      <DeployMateDialog open onClose={() => {}} onLaunch={() => {}} launching={false} region="us-west-2" />,
    )

    await u.click(await screen.findByRole('option', { name: 'orchard-sde', exact: true }))

    expect(screen.getByText('Serves demo')).toBeInTheDocument()
    expect(
      screen.getByText(/set up for demo only, so it cannot deploy orchard-sde/i),
    ).toBeInTheDocument()
    expect(screen.getByTestId('deploy-mate-continue')).toBeDisabled()
  })
})
