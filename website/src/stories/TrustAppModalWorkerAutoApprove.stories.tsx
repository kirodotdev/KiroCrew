import type { Meta, StoryObj } from '@storybook/react-vite'
import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'

/**
 * The trust prompt for an app that declares `permissions.workerAutoApprove`: the grant
 * gets its own box outside the three-row ceiling, beside a `sessionApproval` one when an
 * app declares both. Use the theme toolbar for dark and light.
 */
const APP: TrustAppTarget = {
  name: 'crew-runner',
  displayName: 'Crew Runner',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/crew-runner',
  retryInstalls: true,
  workerAutoApprove: true,
}

const meta: Meta<typeof TrustAppModal> = {
  title: 'App Store/Trust prompt: worker auto-approve',
  component: TrustAppModal,
  args: {
    app: APP,
    pending: false,
    failed: false,
    granted: false,
    onCancel: () => {},
    onConfirm: () => {},
  },
}
export default meta

type Story = StoryObj<typeof TrustAppModal>

export const Declared: Story = {}

export const WithSessionApproval: Story = {
  args: { app: { ...APP, sessionApproval: true } },
}
