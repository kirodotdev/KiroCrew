// Vendor stub: re-exports jsx runtime from the host.
// Names every runtime export of the pinned package, plus the CommonJS-style default
// (appSharedModuleBoundary.test.ts). jsxDEV is not a react/jsx-runtime export; it stays
// so an app that already imports it keeps linking.
const m = window.__kirocrew_modules?.['react/jsx-runtime']
if (!m) throw new Error('[vendor/react-jsx-runtime] Host modules not initialized.')
export const { jsx, jsxs, jsxDEV, Fragment } = m
export default { jsx, jsxs, Fragment }
