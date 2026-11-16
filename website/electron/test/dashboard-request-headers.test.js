"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { dashboardRequestHeaders } = require("../window-lifecycle");

describe("dashboardRequestHeaders", () => {
  const PAGE = "http://localhost:7778/apps/mood-music?token=secret#x";

  it("gives an embedded frame the dashboard origin only", () => {
    const headers = dashboardRequestHeaders({
      resourceType: "subFrame",
      requestHeaders: { Referer: PAGE, Accept: "text/html" },
    });
    assert.deepEqual(headers, { Referer: "http://localhost:7778/", Accept: "text/html" });
  });

  it("sends no Referer on any other request", () => {
    for (const resourceType of ["mainFrame", "script", "image", "xhr", "media", "other"]) {
      const headers = dashboardRequestHeaders({ resourceType, requestHeaders: { Referer: PAGE } });
      assert.deepEqual(headers, {}, resourceType);
    }
  });

  it("adds none where the page sent none or its origin is opaque", () => {
    assert.deepEqual(dashboardRequestHeaders({ resourceType: "subFrame", requestHeaders: {} }), {});
    for (const Referer of ["about:blank", "not a url"]) {
      assert.deepEqual(dashboardRequestHeaders({ resourceType: "subFrame", requestHeaders: { Referer } }), {});
    }
  });
});
