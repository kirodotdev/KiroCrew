"use strict";

const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness } = require("./gateway-supervisor-test-helpers");

// An http fake that holds every probe until the test releases it, so a test can
// change state while startGateway is waiting on its port probes. Released and
// later probes are refused, which is the "nothing answered" path that spawns.
function heldHttp() {
  const held = [];
  let released = false;
  const refuse = (request) => request.emit("error", new Error("connection refused"));
  return {
    held,
    release() {
      released = true;
      for (const request of held.splice(0)) refuse(request);
    },
    get() {
      const request = new EventEmitter();
      request.destroy = () => {};
      if (released) queueMicrotask(() => refuse(request));
      else held.push(request);
      return request;
    },
  };
}

test("a quit that begins while startGateway is probing the port spawns nothing", async () => {
  let quitting = false;
  const http = heldHttp();
  const { supervisor, spawnCalls, logs } = harness({
    httpMod: http,
    isQuitting: () => quitting,
  });

  const started = supervisor.start();
  await flush();
  assert.ok(http.held.length > 0, "startGateway is waiting on a port probe");
  assert.strictEqual(spawnCalls.length, 0);

  quitting = true;
  http.release();

  assert.strictEqual(await started, false);
  assert.strictEqual(spawnCalls.length, 0, "no gateway is spawned once quitting has begun");
  assert.ok(logs.some((line) => line.includes("the app is quitting")));
});

test("the same start with no quit still spawns the gateway", async () => {
  const http = heldHttp();
  const { supervisor, spawnCalls } = harness({
    httpMod: http,
    isQuitting: () => false,
  });

  const started = supervisor.start();
  await flush();
  assert.ok(http.held.length > 0, "startGateway is waiting on a port probe");
  http.release();

  assert.strictEqual(await started, true);
  assert.strictEqual(spawnCalls.length, 1);
});

test("a start called after quitting has begun spawns nothing", async () => {
  const { supervisor, spawnCalls } = harness({ isQuitting: () => true });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0);
});
