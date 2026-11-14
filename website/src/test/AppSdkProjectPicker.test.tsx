/**
 * The App SDK's `ProjectPicker` mounts only for an app that declares what it calls.
 *
 * An external app gets the dashboard's picker once its `permissions.api` admits
 * every path the picker sends (`PROJECT_PICKER_PATHS`); with one missing it
 * renders a refusal naming the path, with no chat hand-off, and mounts nothing. A builtin page
 * gets the picker as before, and a surface with no app identity fails closed.
 *
 * The host picker is stubbed here: what is under test is which surfaces mount it
 * and with what props. The last block pins the other half of the claim against
 * the real picker's source and the real dashboard client: every path the picker
 * can send is one the check at mount reads, so that check covers every request.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

const hostPicker = vi.hoisted(() => ({ props: [] as Record<string, unknown>[] }))
vi.mock('../components/ProjectPicker', () => ({
  default: (props: Record<string, unknown>) => {
    hostPicker.props.push(props)
    return <div data-testid="host-project-picker" />
  },
}))

import { AppApiProvider, ProjectPicker } from '../app-sdk/index'
import { AppIdentityProvider } from '../app-sdk/identity'
import { i18nT } from '../i18n/t'
import { PROJECT_PICKER_PATHS, undeclaredProjectPickerPath } from '../app-sdk/ProjectPicker'
import pickerSource from '../components/ProjectPicker.tsx?raw'
import newFolderSource from '../components/ProjectPickerNewFolder.tsx?raw'

let fetchMock: ReturnType<typeof vi.fn>
let appSeq = 0

beforeEach(() => {
  hostPicker.props.length = 0
  fetchMock = vi.fn(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  appSeq += 1
  return (
    <AppApiProvider appName={`picker-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

const onSelect = vi.fn()
const onOpenChange = vi.fn()
const picker = (open = true) => (
  <ProjectPicker open={open} onOpenChange={onOpenChange} onSelect={onSelect} startPath="/work/repo" />
)

describe('undeclaredProjectPickerPath', () => {
  it('names the first path a declaration does not admit', () => {
    expect(undeclaredProjectPickerPath([])).toBe('/api/browse-dirs')
    expect(undeclaredProjectPickerPath(['/api/browse-dirs'])).toBe('/api/recent-projects')
    expect(undeclaredProjectPickerPath(['/api/browse-dirs', '/api/recent-projects'])).toBe('/api/favorite-projects')
    // A child of a family is not the family: it would leave the listing undeclared.
    expect(undeclaredProjectPickerPath(['/api/browse-dirs/create', '/api/recent-projects', '/api/favorite-projects'])).toBe('/api/browse-dirs')
  })

  it('is null once every path is admitted, by the three bare families or a wildcard', () => {
    expect(undeclaredProjectPickerPath([...PROJECT_PICKER_PATHS])).toBeNull()
    expect(undeclaredProjectPickerPath(['/api/browse-dirs', '/api/recent-projects', '/api/favorite-projects'])).toBeNull()
    expect(undeclaredProjectPickerPath(['/api/*'])).toBeNull()
  })

  it('does not let an exact listing entry admit the New folder write beneath it', () => {
    expect(undeclaredProjectPickerPath(['=/api/browse-dirs', '/api/recent-projects', '/api/favorite-projects']))
      .toBe('/api/browse-dirs/create')
  })
})

describe('the App SDK ProjectPicker', () => {
  it('refuses an external app that declares none of the families, and mounts nothing', () => {
    render(externalApp(['/api/project-scaffold'], picker()))
    expect(screen.getByTestId('app-project-picker-not-declared')).toHaveTextContent('/api/browse-dirs')
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('offers no chat hand-off on the refusal, which would unmount the unsaved form', () => {
    render(externalApp([], picker()))
    const notice = screen.getByTestId('app-project-picker-not-declared')
    expect(notice).not.toHaveTextContent(i18nT('components.askAgent.ask_the_agent'))
    expect(notice.querySelector('button')).toBeNull()
  })

  it('refuses an exact listing entry, naming the New folder path, and mounts nothing', () => {
    render(externalApp(['=/api/browse-dirs', '/api/recent-projects', '/api/favorite-projects'], picker()))
    expect(screen.getByTestId('app-project-picker-not-declared')).toHaveTextContent('/api/browse-dirs/create')
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })

  it('refuses an external app that declares only some of them, naming the missing one', () => {
    render(externalApp(['/api/browse-dirs', '/api/favorite-projects'], picker()))
    expect(screen.getByTestId('app-project-picker-not-declared')).toHaveTextContent('/api/recent-projects')
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })

  it('fails closed with no app identity and no SDK provider', () => {
    render(picker())
    expect(screen.getByTestId('app-project-picker-not-declared')).toBeInTheDocument()
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })

  it('mounts the dashboard picker for an external app that declares every family', () => {
    render(externalApp([...PROJECT_PICKER_PATHS], picker()))
    expect(screen.getByTestId('host-project-picker')).toBeInTheDocument()
    expect(hostPicker.props).toHaveLength(1)
    const props = hostPicker.props[0]
    expect(props.onSelect).toBe(onSelect)
    expect(props.onOpenChange).toBe(onOpenChange)
    expect(props.startPath).toBe('/work/repo')
    expect(props.open).toBe(true)
    // The chat hand-off would unmount the app page under the user.
    expect(props.errorHandoff).toBeUndefined()
    expect(props.footer).toBeUndefined()
  })

  it('mounts the dashboard picker for a builtin page whatever it declares', () => {
    render(<AppIdentityProvider appId="project-scaffolder" origin="builtin">{picker()}</AppIdentityProvider>)
    expect(screen.getByTestId('host-project-picker')).toBeInTheDocument()
  })

  it('renders nothing while closed, refusal included', () => {
    render(externalApp([], picker(false)))
    expect(screen.queryByTestId('app-project-picker-not-declared')).toBeNull()
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })
})

describe('the check at mount reads every path the dashboard picker can send', () => {
  // The dashboard client members the picker and its New folder form call.
  const PICKER_MEMBERS = [
    'addFavoriteProject',
    'browseDirs',
    'browseDrives',
    'createBrowseDir',
    'favoriteProjects',
    'recentProjects',
    'removeFavoriteProject',
  ]

  it('names exactly the members the picker source calls', () => {
    const called = new Set<string>()
    for (const source of [pickerSource, newFolderSource]) {
      for (const match of source.matchAll(/\bapi\.(\w+)\(/g)) called.add(match[1])
    }
    expect([...called].sort()).toEqual(PICKER_MEMBERS)
  })

  it('sends each member to a path the check reads itself, never only its parent', async () => {
    const { api } = (await vi.importActual('../api/client')) as { api: Record<string, (...args: string[]) => Promise<unknown>> }
    for (const member of PICKER_MEMBERS) {
      fetchMock.mockClear()
      await api[member]('/work/repo', 'child').catch(() => undefined)
      expect(fetchMock, member).toHaveBeenCalled()
      for (const call of fetchMock.mock.calls) {
        const path = new URL(String(call[0]), 'http://localhost').pathname
        expect(PROJECT_PICKER_PATHS as readonly string[], `${member} -> ${path}`).toContain(path)
      }
    }
  })
})
