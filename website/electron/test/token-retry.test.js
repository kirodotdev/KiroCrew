const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTokenRetryHandler } = require("../token-retry");

describe("createTokenRetryHandler", () => {
  it("calls refreshFn on 403", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    assert.equal(called, 1);
  });

  it("does not call refreshFn on 200", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(200);
    assert.equal(called, 0);
  });

  it("stops after maxRetries (default 2)", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    await handler(403);
    await handler(403); // should be ignored
    await handler(403); // should be ignored
    assert.equal(called, 2);
  });

  it("resets retries on 200", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    await handler(403);
    assert.equal(called, 2);
    await handler(200); // reset
    await handler(403); // should work again
    assert.equal(called, 3);
  });

  it("respects custom maxRetries", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; }, 1);
    await handler(403);
    await handler(403); // should be ignored
    assert.equal(called, 1);
  });

  it("ignores other status codes", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(301);
    await handler(404);
    await handler(500);
    assert.equal(called, 0);
  });
});
