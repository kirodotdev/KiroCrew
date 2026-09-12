#!/usr/bin/env node
// Runs the claude CLI for the claude-agent-acp adapter without asking for the
// bypassPermissions capability.
//
// The adapter sets the Agent SDK option allowDangerouslySkipPermissions for
// every session that is not running as root, and the SDK turns that into
// --allow-dangerously-skip-permissions on the CLI command line. That flag is
// the capability to enter bypassPermissions, the one permission mode in which
// the adapter stops sending session/request_permission, so a session in it
// never reaches the host gate. Kiro Crew never selects that mode, so this
// launcher drops the flag before it starts the real CLI. A CLI that refuses
// the flag in its environment (it does so as root, for example) then starts
// normally instead of failing session/new.
//
// Everything else passes through: the remaining arguments in order, stdio,
// and the environment. An explicit --permission-mode is not rewritten.
//
// Wiring (acp/client.py): CLAUDE_CODE_EXECUTABLE points here, which makes the
// SDK run this file with node, and KIROCREW_CLAUDE_CODE_EXECUTABLE names the
// real CLI. The child sees CLAUDE_CODE_EXECUTABLE set back to the real CLI and
// no KIROCREW_CLAUDE_CODE_EXECUTABLE, so its environment is the one it would
// have had without this hop.
import { spawn } from 'node:child_process'

const TARGET_ENV = 'KIROCREW_CLAUDE_CODE_EXECUTABLE'
const DROPPED_FLAGS = new Set(['--allow-dangerously-skip-permissions'])
// A JavaScript entry point cannot be exec'd on every platform, so run it with
// this node, the way the SDK itself runs a script-valued executable.
const SCRIPT_SUFFIXES = ['.js', '.mjs', '.cjs']
// Exit status when the real CLI could not be started at all.
const EXIT_CANNOT_EXECUTE = 127

const target = process.env[TARGET_ENV]
if (!target) {
  process.stderr.write(`claude launcher: ${TARGET_ENV} is not set\n`)
  process.exit(EXIT_CANNOT_EXECUTE)
}

const args = process.argv.slice(2).filter((arg) => !DROPPED_FLAGS.has(arg))
const env = { ...process.env, CLAUDE_CODE_EXECUTABLE: target }
delete env[TARGET_ENV]

const isScript = SCRIPT_SUFFIXES.some((suffix) => target.toLowerCase().endsWith(suffix))
const command = isScript ? process.execPath : target
const argv = isScript ? [target, ...args] : args

const child = spawn(command, argv, { stdio: 'inherit', env, windowsHide: true })

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(signal, () => {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal)
  })
}

child.on('error', (err) => {
  process.stderr.write(`claude launcher: cannot start ${target}: ${err.message}\n`)
  process.exit(EXIT_CANNOT_EXECUTE)
})

child.on('exit', (code, signal) => {
  if (signal) {
    // Die the way the child died, so the SDK sees the same termination.
    process.removeAllListeners(signal)
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 1)
})
