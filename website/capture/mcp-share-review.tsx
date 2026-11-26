// Component capture for the MCP Servers approval card, the servers-list share
// notice under it, and one Review dialog block, with mocked data. `?scene=`
// picks `page` (card + notice), `review` (one block's launch columns), `value`
// (a launch whose PATH value changed), `dialog`
// (the Review dialog) or `row` (two shared, approved Sharing rows: one trusted,
// one not). `?w=` sets the width.
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n/all'
import { api, type McpManagedServer } from '../src/api/client'
import McpShareRefusedNotice from '../src/pages/overview/McpShareRefusedNotice'
import { ApprovalCard, ReviewApprovalsDialog } from '../src/pages/settings/McpManagementHero'
import { RefusalLaunch, ServerName, ShareAction, TrustChangesChoice } from '../src/pages/settings/McpManagementLaunchReview'
import type { GatewayStatus, LaunchRefusal } from '../src/pages/settings/mcpManagementModel'
import '../src/index.css'

const scene = new URLSearchParams(location.search).get('scene') || 'page'
const names = ['artifactory-mcp', 'builder-mcp', 'grasp-mcp', 'local-chorus-mcp', 'slack-mcp']
const approvable = { reason: 'changed_needs_reapproval', complete: true, expected_launch: 'a:b' }
const status: GatewayStatus = {
  enabled: true,
  stub: [...names, 'docs-mcp'],
  stub_count: 6,
  running: true,
  ping_ok: true,
  supported: true,
  launch_refused: {
    ...Object.fromEntries(names.map(n => [n, approvable])),
    'docs-mcp': { reason: 'too_many_launches', complete: false, launch_cap: 32 },
  },
  share_refused: { ...Object.fromEntries(names.map(n => [n, 'changed_needs_reapproval'])), 'docs-mcp': 'too_many_launches' },
}
api.mcpGatewayStatus = async () => status

// Three agents each give the server their own PATH; the approved and new
// launches hold the same three values, as a store written before the PATH fold does.
const CMD = ['/home/me/.toolbox/bin/artifactory-mcp']
const paths = ['PATH=/home/me/.local/bin:/usr/bin', 'PATH=/home/me/.cargo/bin:/usr/bin', 'PATH=/home/me/bin:/usr/bin:/sbin']
const perAgent: LaunchRefusal = {
  reason: 'changed_needs_reapproval',
  complete: true,
  expected_launch: 'shown',
  commands: [CMD, CMD, CMD],
  envs: [[paths[0]], [paths[1]], [paths[2]]],
  approved_commands: [CMD, CMD, CMD],
  approved_envs: [[paths[1]], [paths[2]], [paths[0]]],
}

// A launch whose PATH value changed while its displayed copy did not, so the
// gateway names the key in `changed_env_keys`.
const changedValue: LaunchRefusal = {
  reason: 'changed_needs_reapproval',
  complete: true,
  expected_launch: 'shown',
  commands: [CMD],
  envs: [[paths[0]]],
  approved_commands: [CMD],
  approved_envs: [[paths[0]]],
  changed_env_keys: ['PATH'],
}

// A changed command line, as the Review dialog shows it.
const changedCmd: LaunchRefusal = {
  reason: 'changed_needs_reapproval',
  complete: true,
  expected_launch: 'shown',
  commands: [[...CMD, '--region', 'us-west-2']],
  envs: [[paths[0]]],
  approved_commands: [CMD],
  approved_envs: [[paths[0]]],
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
qc.setQueryData(['mcpGatewayStatus'], status)
document.documentElement.dataset.theme = 'kiro-dark'
await initI18n()

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <MemoryRouter>
      <main className="bg-bg text-text min-h-screen p-6" style={{ maxWidth: Number(new URLSearchParams(location.search).get('w') || 420) }} data-capture-root>
        <p className="text-[13px] text-muted mb-4">Component render, not the live page</p>
        {scene === 'dialog' ? (
          <ReviewApprovalsDialog
            open
            supported
            items={[{ server: { name: 'artifactory-mcp', stub: true, can_stub: true, agents: ['a'] } as McpManagedServer, refusal: changedCmd }]}
            onApprove={async () => null}
            onClose={() => {}}
          />
        ) : scene === 'row' ? (
          // The Sharing table's row markup: a two-column grid below md, a table row from md.
          <table className="w-full border-collapse">
            <tbody>
              {[
                { name: 'artifactory-mcp', trusted: true },
                { name: 'company-internal-knowledge-search-mcp', trusted: false },
              ].map(({ name, trusted }) => {
                const s = { name, stub: true, can_stub: true, in_allowlist: true, entry_poolable: false, agents: ['a'], transport: 'stdio', denylisted: false, trust_changes: trusted } as McpManagedServer
                return (
                  <tr key={name} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 border-t border-[var(--border)] px-3 py-3 md:table-row md:px-0 md:py-0">
                    <td className="col-start-1 row-start-1 min-w-0 p-0 align-top md:table-cell md:px-4 md:py-3">
                      <ServerName id={`n-${name}`} name={name} />
                      <TrustChangesChoice server={s} approved busy={false} onTrustChange={() => {}} />
                    </td>
                    <td className="col-start-2 row-start-1 p-0 text-right align-top md:table-cell md:px-4 md:py-2.5">
                      <ShareAction server={s} busy={false} reading={false} supported onShare={() => {}} onConfirm={() => {}} onStop={() => {}} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : scene === 'review' ? (
          <section className="rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2.5 text-[12px] text-[var(--muted)]">
            <h3 className="m-0 mb-2 font-mono text-[13px] font-semibold text-[var(--text)]">artifactory-mcp</h3>
            <RefusalLaunch refusal={perAgent} />
          </section>
        ) : scene === 'value' ? (
          <section className="rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2.5 text-[12px] text-[var(--muted)]">
            <h3 className="m-0 mb-2 font-mono text-[13px] font-semibold text-[var(--text)]">artifactory-mcp</h3>
            <RefusalLaunch refusal={changedValue} />
          </section>
        ) : (
          <div className="space-y-4">
            <ApprovalCard waiting={names.map(name => ({ name }) as McpManagedServer)} onReview={() => {}} />
            <McpShareRefusedNotice />
          </div>
        )}
      </main>
    </MemoryRouter>
  </QueryClientProvider>,
)
