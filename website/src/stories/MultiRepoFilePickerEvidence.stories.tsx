import { useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import FilePickerMenu from '../components/FilePickerMenu'

/** Visual fixture for the multi-repo `@`-mention picker: the file search fans
 *  across the folder's primary project and its extra repos, so a result list
 *  can mix files from more than one repository. The results come from
 *  `/api/file-search` — the capture script routes that endpoint to a fixed
 *  payload spanning two repos (no live gateway), which is what lets this render
 *  a representative list. The picker is anchored to a fake composer input so
 *  the menu's geometry matches the real composer. */
function MultiRepoFilePickerEvidence() {
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  )
  const anchorRef = useRef<HTMLInputElement>(null)
  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ position: 'relative', width: 640, margin: '0 auto', paddingTop: 320 }}>
        <input
          ref={anchorRef}
          readOnly
          value="@platform"
          aria-label="composer"
          className="w-full rounded-lg border border-border bg-bg-elevated px-3 py-2 font-mono text-[13px] text-text"
        />
        <FilePickerMenu
          query="platform"
          anchorRef={anchorRef}
          open
          project="/Users/you/code/platform-api"
          repos={[
            '/Users/you/code/platform-web-client-application',
            '/Users/you/code/platform-shared-design-system-and-tokens',
          ]}
          onSelect={() => {}}
          onClose={() => {}}
        />
      </div>
    </QueryClientProvider>
  )
}

const meta = {
  title: 'Evidence/Multi-repo file picker',
  component: MultiRepoFilePickerEvidence,
} satisfies Meta<typeof MultiRepoFilePickerEvidence>

export default meta
type Story = StoryObj<typeof meta>

export const MixedResults: Story = {}
