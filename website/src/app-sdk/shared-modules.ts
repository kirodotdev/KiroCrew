/**
 * Shared module registry for federated app loading.
 *
 * The host app registers its React, ReactDOM, lucide-react, and app-sdk
 * instances here at startup. Dynamically imported app bundles access them
 * via the import map → /vendor/*.mjs stubs → this registry.
 *
 * This avoids duplicate React instances (which break hooks) and ensures
 * apps share the exact same module instances as the host.
 */

import * as React from 'react'
import * as ReactDOM from 'react-dom'
import * as jsxRuntime from 'react/jsx-runtime'
import * as lucideReact from 'lucide-react'
import * as reactQuery from '@tanstack/react-query'
import * as appSdk from './index'
import * as kirocrewUi from '../kirocrew-ui'

// Register on window for vendor stubs to access
declare global {
  interface Window {
    __kirocrew_modules: {
      react: typeof React
      'react-dom': typeof ReactDOM
      'react/jsx-runtime': typeof jsxRuntime
      'lucide-react': typeof lucideReact
      '@tanstack/react-query': typeof reactQuery
      '@kirocrew/app-sdk': typeof appSdk
      '@kirocrew/ui': typeof kirocrewUi
    }
  }
}

window.__kirocrew_modules = {
  react: React,
  'react-dom': ReactDOM,
  'react/jsx-runtime': jsxRuntime,
  'lucide-react': lucideReact,
  '@tanstack/react-query': reactQuery,
  '@kirocrew/app-sdk': appSdk,
  '@kirocrew/ui': kirocrewUi,
}
