import { afterEach, describe, expect, it } from 'vitest'
import { queryClient } from '../../api/queryClient'
import { cachedViewerIds } from './cachedViewer'
import { sharedCrewKeys } from './sharedCrewSocket'

const viewer = (id: string) => ({ id, kind: 'human', display: { name: 'Me', login: '' } })

describe('cachedViewerIds', () => {
  afterEach(() => { queryClient.removeQueries({ queryKey: sharedCrewKeys.mode }) })

  it('is empty until the mode has answered', () => {
    expect(cachedViewerIds()).toEqual([])
  })

  it("lists every id the server says names the viewer", () => {
    queryClient.setQueryData(sharedCrewKeys.mode, { viewer: viewer('local:local-startup'), viewer_ids: ['local:local-app', 'local:local-startup'] })
    expect(cachedViewerIds()).toEqual(['local:local-app', 'local:local-startup'])
  })

  it('falls back to the viewer id when the server lists none', () => {
    queryClient.setQueryData(sharedCrewKeys.mode, { viewer: viewer('relay:ana') })
    expect(cachedViewerIds()).toEqual(['relay:ana'])
  })
})
