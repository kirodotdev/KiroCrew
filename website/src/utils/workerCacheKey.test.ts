import { workerUrlWithBuildKey } from './workerCacheKey'

describe('workerUrlWithBuildKey', () => {
  // The whole point of the key is that this build's worker URL differs from the
  // bare content-hashed URL a browser can hold under the year-long immutable
  // policy, so the browser leaves the stranded entry behind and re-fetches under
  // the short-lived policy. Assert the key is present and non-empty.
  it('appends a non-empty build-identity query to a bare URL', () => {
    const out = workerUrlWithBuildKey(new URL('https://host/assets/diffWorker-abcd.js'))
    const parsed = new URL(out)
    expect(parsed.pathname).toBe('/assets/diffWorker-abcd.js')
    expect(parsed.searchParams.get('v')).toBeTruthy()
    expect(out).toBe(`https://host/assets/diffWorker-abcd.js?v=${encodeURIComponent(__APP_VERSION__)}`)
  })

  it('accepts a string URL as well as a URL object', () => {
    const out = workerUrlWithBuildKey('https://host/assets/hljsWorker-ef01.js')
    expect(new URL(out).searchParams.get('v')).toBe(__APP_VERSION__)
  })

  it('uses & when the URL already carries a query, keeping the existing param', () => {
    const out = workerUrlWithBuildKey('https://host/assets/worker-portable-ff.js?type=module')
    const parsed = new URL(out)
    expect(parsed.searchParams.get('type')).toBe('module')
    expect(parsed.searchParams.get('v')).toBe(__APP_VERSION__)
    expect(out).toContain('?type=module&v=')
  })

  it('does not alter the path, so the server still resolves it to the emitted chunk', () => {
    // The server classifies /assets/*worker*.js by request.path (query stripped)
    // and add_static resolves the file by path alone, so the query key never
    // changes which file is served or how it is classified.
    const out = workerUrlWithBuildKey(new URL('https://host/assets/subset-worker.chunk-9a.js'))
    expect(new URL(out).pathname).toBe('/assets/subset-worker.chunk-9a.js')
  })
})
