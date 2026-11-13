import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import EmbedQuestionCard, { type EmbedPendingQuestion } from '../app-sdk/EmbedQuestionCard'
import type { AppApi } from '../app-sdk/scopedApi'

/**
 * Visual evidence for #25371: the native mid-turn `AskUserQuestion` card as the
 * app-sdk `ChatEmbed` now shows it (a `card_id` + `native: true`, no `ask_id`).
 * The rendering is the shared `QuestionCard`; this story stands it in a narrow
 * embed-width frame so a reviewer can see it in dark and light with the theme
 * toolbar. The `api` is a no-op stub — the story renders the card, it does not
 * post a steer.
 */
const noopApi: AppApi = {
  get: async () => ({}) as never,
  post: async () => ({ ok: true, steered: true }) as never,
} as unknown as AppApi

const NATIVE_CARD: EmbedPendingQuestion = {
  card_id: 'card-1',
  slot: 'demo',
  native: true,
  ts: 1,
  questions: [
    {
      question: 'Which environment should I deploy to?',
      header: 'DEPLOY TARGET',
      options: [
        { label: 'Staging', description: 'safe, auto-torn-down' },
        { label: 'Production', description: 'live traffic' },
      ],
    },
  ],
}

function Frame({ pending }: { pending: EmbedPendingQuestion | null }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      {/* A representative embed column: narrow, bordered, the card at its foot. */}
      <div
        style={{
          width: 420,
          border: '1px solid var(--border)',
          borderRadius: 10,
          background: 'var(--bg)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: 13 }}>
          Embedded chat — the agent is waiting on a mid-turn question.
        </div>
        <EmbedQuestionCard api={noopApi} slot="demo" pending={pending} loadFailed={false} onRestore={() => true} />
      </div>
    </QueryClientProvider>
  )
}

const meta = {
  title: 'AppSdk/EmbedQuestionCard',
  component: EmbedQuestionCard,
  // The stories render through `Frame`, which supplies the real props; these
  // satisfy the component's required prop types for the story typing.
  args: { api: noopApi, slot: 'demo', pending: NATIVE_CARD, loadFailed: false, onRestore: () => true },
} satisfies Meta<typeof EmbedQuestionCard>

export default meta
type Story = StoryObj<typeof meta>

/** The native mid-turn card, as the embed now shows it. */
export const NativeMidTurnCard: Story = {
  render: () => <Frame pending={NATIVE_CARD} />,
}
