import { useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FolderOpen, Plus } from 'lucide-react'

import FolderConfigModal from '../components/FolderConfigModal'
import { Btn } from '../components/ui'
import { i18nT } from '../i18n/t'
import type { ChatFolder } from '../types'

/** Visual fixture for the multi-repo "Additional repositories" editor in the
 *  folder settings modal (the only user-visible surface of the multi-repo
 *  project feature). The modal makes no network call while the ProjectPicker
 *  stays closed, so a bare QueryClientProvider is enough; the picker itself is
 *  covered by FolderConfigModal.test.tsx. Two states are captured — a project
 *  with no extra repos (just the "Add repository" button and the hint) and one
 *  with three long absolute paths — in both dark and light via the preview's
 *  themeOverride. */
type State = 'empty' | 'filled'

const PRIMARY = '/Users/you/code/platform-api'

const filled: ChatFolder = {
  id: 'platform',
  name: 'Platform rewrite',
  order: 0,
  collapsed: false,
  parent_id: '',
  project_dir: PRIMARY,
  repos: [
    '/Users/you/code/platform-web-client-application',
    '/Users/you/code/platform-shared-design-system-and-tokens',
    '/Users/you/code/platform-infrastructure-terraform',
  ],
} as ChatFolder

const empty: ChatFolder = {
  id: 'platform',
  name: 'Platform rewrite',
  order: 0,
  collapsed: false,
  parent_id: '',
  project_dir: PRIMARY,
} as ChatFolder

function FolderConfigModalEvidence({ state }: { state: State }) {
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  )
  // The palette (dark/light) is driven by the preview decorator from the
  // Storybook `theme` global (set per capture via ?globals=theme:dark|light),
  // so the modal's portal inherits the document `data-theme`/`data-mode` the
  // decorator writes. Nothing to force here.
  const folder = state === 'empty' ? empty : filled
  return (
    <QueryClientProvider client={queryClient}>
      <FolderConfigModal
        open
        mode="edit"
        folder={folder}
        folders={[folder]}
        installedAgents={[{ name: 'kirocrew' }, { name: 'kirocrew-dev' }]}
        globalDefaultAgent="kirocrew"
        onClose={() => {}}
        onSubmit={async () => {}}
        onRetryTags={() => {}}
      />
    </QueryClientProvider>
  )
}

const meta = {
  title: 'Evidence/Folder multi-repo editor',
  component: FolderConfigModalEvidence,
  args: { state: 'filled' },
} satisfies Meta<typeof FolderConfigModalEvidence>

export default meta
type Story = StoryObj<typeof meta>

// Palette (dark/light) is selected at capture time via the Storybook `theme`
// global, so one story per content state suffices.
export const Filled: Story = { args: { state: 'filled' } }
export const Empty: Story = { args: { state: 'empty' } }

/** The inline no-op note: shown under "Add repository" when a pick adds no row
 *  (the picked path is already the project directory, or already in the list).
 *  Reproduces the exact element the modal renders — the real i18n string in the
 *  real `text-[11px] text-warn` style under the real add button — because the
 *  note is transient internal state a static story cannot drive through the
 *  ProjectPicker. Its behaviour is pinned by FolderConfigModal.test.tsx. */
function NoOpNoteEvidence() {
  return (
    <div className="flex flex-col gap-1.5" style={{ width: 640 }}>
      <span className="text-[11.5px] font-semibold text-muted">
        {i18nT('components.folderConfigModal.repos')}
      </span>
      <div className="flex items-center gap-2 bg-bg-elevated border border-border rounded-lg px-2.5 py-1.5">
        <FolderOpen size={12} className="text-accent shrink-0" />
        <span className="flex-1 min-w-0 font-mono text-[12px] text-text truncate">
          /Users/you/code/platform-web-client-application
        </span>
      </div>
      <div>
        <Btn>
          <Plus size={13} /> {i18nT('components.folderConfigModal.repo_add')}
        </Btn>
      </div>
      <span data-testid="folder-config-repo-note" className="text-[11px] text-warn">
        {i18nT('components.folderConfigModal.repo_is_project_dir')}
      </span>
      <span className="text-[11px] text-muted-strong">
        {i18nT('components.folderConfigModal.repos_hint')}
      </span>
    </div>
  )
}

const noteMeta = {
  title: 'Evidence/Folder multi-repo editor',
  component: NoOpNoteEvidence,
} satisfies Meta<typeof NoOpNoteEvidence>

export const NoOpNote: StoryObj<typeof noteMeta> = { render: () => <NoOpNoteEvidence /> }
