import type { Meta, StoryObj } from '@storybook/react-vite'
import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'

/**
 * The trust prompt for an app whose `permissions.api` reaches past its own
 * namespace: those entries get a box of their own, listed as declared, and the
 * app's own `/apps/<name>/...` entries are left out. Use the theme toolbar for
 * dark and light.
 */
const APP: TrustAppTarget = {
  name: 'design-tweak',
  displayName: 'Design Tweak',
  origin: 'registry',
  trustRepository: 'https://github.com/KiroCrewCaptain/kirocrew-app-design-tweak',
  retryInstalls: true,
  api: ['/apps/design-tweak/api', '/apps/design-tweak/api/*', '/api/chat'],
}

const meta: Meta<typeof TrustAppModal> = {
  title: 'App Store/Trust prompt: host endpoints',
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

export const SeveralPaths: Story = {
  args: { app: { ...APP, api: [...(APP.api ?? []), '/api/source/pull-request'] } },
}
