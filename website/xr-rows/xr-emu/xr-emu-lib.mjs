// Node helpers for Playwright scripts that drive the IWER emulator.
// Usage:
//   import { installEmulator, emu } from '.../xr-emu/xr-emu-lib.mjs';
//   await installEmulator(context);           // before page.goto
//   await emu.setHead(page, { position: [0, 1.6, 0] });
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const INIT_SCRIPT_PATH = join(here, 'iwer-init.js');

/** Adds iwer-init.js as an init script on every page/frame of the context.
 * Init scripts run before page scripts and are not subject to the page CSP. */
export async function installEmulator(context) {
  await context.addInitScript({ content: readFileSync(INIT_SCRIPT_PATH, 'utf8') });
}

const call = (name) => (page, arg) =>
  page.evaluate(([n, a]) => window.__xrEmu[n](a), [name, arg ?? null]);

export const emu = {
  info: call('info'),
  setHead: call('setHead'),            // { position:[x,y,z], yawDeg, pitchDeg }
  setInputMode: call('setInputMode'),  // 'controller' | 'hand'
  setRay: call('setRay'),              // { hand, origin:[..], direction:[..], rollDeg? }
  select: call('select'),              // { hand, down:true|false }
  cancelSelect: call('cancelSelect'),  // { hand }
  setHandPose: call('setHandPose'),    // { hand, position:[..], palmTowardHead, fingers?, clear? }
  setHandShape: call('setHandShape'),  // { hand, pose?: 'default'|'point'|'pinch'|... } -> { pose, poses }
  disconnect: call('disconnect'),      // { hand }
  reconnect: call('reconnect'),        // { hand }
  frames: call('frames'),              // n XR frames
  end: call('end'),
};

/** Full press: selectstart, wait, then select + selectend. */
export async function click(page, hand, holdFrames = 3) {
  await emu.select(page, { hand, down: true });
  await emu.frames(page, holdFrames);
  await emu.select(page, { hand, down: false });
  await emu.frames(page, 3);
}
