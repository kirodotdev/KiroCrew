// Vendor stub: re-exports ReactDOM/client from the host.
// Names every runtime export of the pinned package, plus the CommonJS-style default
// an app gets from `import client from 'react-dom/client'` (appSharedModuleBoundary.test.ts).
const m = window.__kirocrew_modules?.['react-dom']
if (!m) throw new Error('[vendor/react-dom-client] Host modules not initialized.')
export const { createRoot, hydrateRoot } = m
export default { createRoot, hydrateRoot }
