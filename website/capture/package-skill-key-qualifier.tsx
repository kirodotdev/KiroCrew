/**
 * Capture entry for the PACKAGE SKILL KEY QUALIFIER's user-visible surfaces.
 *
 * Renders AgentSkillsEditor ALONE, as the other capture entries do — and here that is also what
 * makes the shots possible: the full SPA pulls a dependency this sandbox cannot install, while an
 * isolated entry never imports it. `api` is stubbed by mutating the imported module object, so a
 * scene does not depend on which URL or envelope the client uses.
 *
 * Scenes, each an evidence gap the UX lane named: `twins` — two bundles vendoring one relative
 * path, each mapped under its own qualified key with the "Located in …" line saying which copy is
 * bound (unqualified, these collapse into one row and the second is unaddressable); `unmanaged` —
 * hand-authored URIs, their config glyph and the ✕; `warn` — a mapped key whose installed copy is
 * gone, showing the warn chip and the count line.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import AgentSkillsEditor from '../src/components/AgentSkillsEditor'
import { api } from '../src/api/client'

const ALPHA = '/opt/edition/packages/PkgA/eventId-3f9c1a77-ALPHA/skills/shared-skill/SKILL.md'
const OMEGA = '/opt/edition/packages/PkgA/eventId-8b2d4e05-OMEGA/skills/shared-skill/SKILL.md'

/** Two colliding copies plus ordinary rows, so a chip's disambiguator has something to differ from. */
const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR until it is green', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render rich HTML inline', source: 'kirocrew' },
  {
    key: 'package/3f9c1a77c4b0e21d:shared-skill',
    name: 'shared-skill',
    description: 'Vendored by two bundles at the same relative path',
    source: 'package',
    path: ALPHA,
  },
  {
    key: 'package/8b2d4e05a7f1c93b:shared-skill',
    name: 'shared-skill',
    description: 'Vendored by two bundles at the same relative path',
    source: 'package',
    path: OMEGA,
  },
]

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'twins'
if ((params.get('theme') || 'dark') === 'dark') document.documentElement.classList.add('dark')

// The warn scene needs a mapped key the catalog does NOT offer: that is what an uninstalled
// or replaced bundle looks like to the editor, and it is what turns the chip yellow.
const catalogFor = (s: string) =>
  s === 'warn' ? CATALOG.filter(r => r.source !== 'package') : CATALOG

const apiStub = api as unknown as Record<string, unknown>
apiStub.skills = async () => catalogFor(scene)
apiStub.agentPatch = async () => ({ ok: true })

const SCENES: Record<string, { skills: string[]; unmanaged?: string[] }> = {
  twins: {
    skills: ['package/3f9c1a77c4b0e21d:shared-skill', 'package/8b2d4e05a7f1c93b:shared-skill', 'babysit'],
  },
  unmanaged: {
    skills: ['babysit'],
    unmanaged: ['skill://local/hand-authored/release-notes', 'skill://local/team-internal/oncall-drill'],
  },
  warn: {
    skills: ['package/3f9c1a77c4b0e21d:shared-skill', 'babysit'],
  },
}

const chosen = SCENES[scene] ?? SCENES.twins
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <div
      data-testid={`scene-${scene}`}
      style={{ padding: 24, width: 760, background: 'var(--bg)', color: 'var(--text)' }}
    >
      <AgentSkillsEditor
        agentName="specialist"
        skills={chosen.skills}
        unmanaged={chosen.unmanaged}
        onChange={() => {}}
      />
    </div>
  </QueryClientProvider>,
)
