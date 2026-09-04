import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const PRELOAD_PATH = resolve(process.cwd(), 'electron/preload.js')
const CONTRACT_PATH = resolve(process.cwd(), 'src/types/electron-bridge.d.ts')

const preloadSource = readFileSync(PRELOAD_PATH, 'utf8')

const dashboardBridges = [
  'kirocrew',
  'electronAPI',
  'localGatewayAPI',
  'crashReportsAPI',
  'wslAPI',
  'zoomAPI',
  'browserAPI',
  'updateAPI',
] as const

const bridgeContracts = {
  kirocrew: { name: 'KiroCrewBridge', methods: ['getPathForFile', 'windowControl'] },
  electronAPI: { name: 'ElectronAPI', methods: ['onStatus', 'getAppMenuItems', 'setBadgeCount', 'getGlobalHotkey'] },
  localGatewayAPI: { name: 'LocalGatewayAPI', methods: ['get', 'set'] },
  crashReportsAPI: { name: 'CrashReportsAPI', methods: ['get', 'reveal'] },
  wslAPI: { name: 'WslAPI', methods: ['detect'] },
  zoomAPI: { name: 'ZoomAPI', methods: ['get', 'set', 'step'] },
  browserAPI: { name: 'BrowserAPI', methods: ['open', 'navigate', 'setBounds', 'getState', 'control', 'onDidNavigate'] },
  updateAPI: { name: 'UpdateAPI', methods: ['onState', 'check', 'download', 'install', 'getInfo'] },
} as const

function interfaceBody(contract: string, name: string): string {
  const match = contract.match(new RegExp(`interface ${name} \\{([\\s\\S]*?)\\n  \\}`))
  expect(match, `${name} declaration is missing`).not.toBeNull()
  return match?.[1] ?? ''
}

describe('Electron preload bridge contract', () => {
  it('declares every dashboard bridge exposed by preload.js', () => {
    const contract = readFileSync(CONTRACT_PATH, 'utf8')
    const exposed = [...preloadSource.matchAll(/exposeInMainWorld\("([^"]+)"/g)].map(m => m[1])

    expect(exposed).toEqual(expect.arrayContaining(dashboardBridges))
    for (const bridge of dashboardBridges) {
      expect(contract).toMatch(new RegExp(`\\b${bridge}\\?:`))
      const bridgeContract = bridgeContracts[bridge]
      const body = interfaceBody(contract, bridgeContract.name)
      for (const method of bridgeContract.methods) {
        expect(body).toMatch(new RegExp(`\\b${method}\\b`))
      }
    }
  })

  it('keeps the browser-facing bridge properties optional', () => {
    const contract = readFileSync(CONTRACT_PATH, 'utf8')

    for (const bridge of dashboardBridges) {
      expect(contract).toMatch(new RegExp(`\\b${bridge}\\?:`))
    }
  })
})
