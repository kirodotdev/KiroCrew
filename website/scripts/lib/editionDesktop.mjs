// Fail-closed staging for desktop assets owned by a downstream edition.
//
// The edition never overwrites a core source file. Its allowlisted input is
// copied to a distinct packaged filename, and Electron chooses that file at
// runtime with a stock fallback. Build-desktop.sh removes the staged copy on
// every entry and exit so a later stock build cannot inherit an edition asset.

import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const DESKTOP_SOURCE_FILE = 'loading.html'
const DESKTOP_TARGET_FILE = 'edition-loading.html'
const LOCAL_SHELL_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; " +
  "style-src 'unsafe-inline'; img-src data:; font-src data:; " +
  "connect-src 'none'; media-src 'none'; object-src 'none'; " +
  "frame-src 'none'; child-src 'none'; worker-src 'none'; " +
  "base-uri 'none'; form-action 'none'"

function secureEditionPage(source, sourcePath, parse) {
  const document = parse(source, { sourceCodeLocationInfo: true })
  const html = document.childNodes.find((node) => node.nodeName === 'html')
  const head = html?.childNodes.find((node) => node.nodeName === 'head')
  const insertion = head?.sourceCodeLocation?.startTag?.endOffset
  if (!Number.isInteger(insertion)) {
    throw new Error(`${sourcePath} must contain a <head> element`)
  }
  const meta = `\n  <meta http-equiv="Content-Security-Policy" content="${LOCAL_SHELL_CSP}">`
  return source.slice(0, insertion) + meta + source.slice(insertion)
}

export function stageEditionDesktop({
  editionDir,
  allowEdition,
  electronDir,
}) {
  fs.rmSync(path.join(electronDir, DESKTOP_TARGET_FILE), { force: true })
  if (!editionDir) return []
  if (allowEdition !== '1') {
    throw new Error(
      'KIROCREW_EDITION_DIR is set but KIROCREW_ALLOW_EDITION=1 is not; ' +
      'desktop edition composition is fail-closed',
    )
  }

  const desktopDir = path.join(path.resolve(editionDir), 'desktop')
  if (!fs.existsSync(desktopDir)) return []

  const entries = fs.readdirSync(desktopDir, { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith('.'))
  const strays = entries.filter(
    (entry) => !entry.isFile() || entry.name !== DESKTOP_SOURCE_FILE,
  )
  if (strays.length > 0) {
    throw new Error(
      `${desktopDir} contains entries outside the desktop overlay allowlist ` +
      `(${DESKTOP_SOURCE_FILE}), or non-files: ` +
      strays.map((entry) => entry.name).join(', '),
    )
  }

  if (entries.length === 0) return []
  const destination = path.join(electronDir, DESKTOP_TARGET_FILE)
  const sourcePath = path.join(desktopDir, DESKTOP_SOURCE_FILE)
  const source = fs.readFileSync(sourcePath, 'utf8')
  const requireFromDesktop = createRequire(new URL('../../electron/package.json', import.meta.url))
  const { parse } = requireFromDesktop('parse5')
  fs.writeFileSync(destination, secureEditionPage(source, sourcePath, parse))
  return [destination]
}

function main() {
  const [command, electronDir] = process.argv.slice(2)
  if (!electronDir || command !== 'stage') {
    throw new Error('usage: editionDesktop.mjs stage <electron-dir>')
  }
  const staged = stageEditionDesktop({
    editionDir: process.env.KIROCREW_EDITION_DIR || '',
    allowEdition: process.env.KIROCREW_ALLOW_EDITION || '',
    electronDir,
  })
  for (const file of staged) console.log(`[kirocrew-edition] staged desktop asset: ${file}`)
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
