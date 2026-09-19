/**
 * Run one capture script against a temporary vite dev server, owning the vite
 * child's whole lifecycle in-process: spawn, wait for the port, run the
 * capture, terminate the child. Exists so a harness run needs no shell-level
 * process management around it.
 *
 * Usage: node scripts/run-capture-with-vite.mjs <capture.mjs> <port> [outDir]
 */
import { spawn } from 'node:child_process'
import { connect } from 'node:net'

const [script, portArg, outDir] = process.argv.slice(2)
const port = Number(portArg)
if (!script || !port) {
  console.error('usage: node scripts/run-capture-with-vite.mjs <capture.mjs> <port> [outDir]')
  process.exit(2)
}

const vite = spawn('./node_modules/.bin/vite',
  ['--host', '127.0.0.1', '--port', String(port), '--strictPort'],
  { stdio: 'ignore' })

const waitPort = () => new Promise((resolve, reject) => {
  const started = Date.now()
  const probe = () => {
    const sock = connect(port, '127.0.0.1')
    sock.once('connect', () => { sock.destroy(); resolve() })
    sock.once('error', () => {
      sock.destroy()
      if (Date.now() - started > 30000) return reject(new Error('vite never listened'))
      setTimeout(probe, 500)
    })
  }
  probe()
})

let code = 1
try {
  await waitPort()
  code = await new Promise(resolve => {
    const cap = spawn('node', [script, `http://127.0.0.1:${port}`, ...(outDir ? [outDir] : [])],
      { stdio: 'inherit' })
    cap.on('exit', c => resolve(c ?? 1))
  })
} finally {
  vite.kill('SIGTERM')
}
process.exit(code)
