// Vendor stub: re-exports React from the host's shared module registry.
// App bundles import 'react' → import map resolves to this file → reads from host.
// ESM links named imports before any code runs, so this list must name EVERY
// runtime export of the pinned package; appSharedModuleBoundary.test.ts asserts it.
const m = window.__kirocrew_modules?.react
if (!m) throw new Error('[vendor/react] Host modules not initialized. Ensure shared-modules.ts is loaded.')
export default m
export const {
  useState, useEffect, useRef, useCallback, useMemo, useContext, useReducer,
  useLayoutEffect, useImperativeHandle, useDebugValue, useDeferredValue,
  useTransition, useId, useSyncExternalStore, useInsertionEffect,
  createContext, createElement, cloneElement, createRef, forwardRef, createFactory,
  lazy, memo, startTransition, Fragment, Suspense, StrictMode, Profiler,
  Children, Component, PureComponent, isValidElement, act, unstable_act, version,
  __SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED,
} = m
