"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { NOTICE, NOTICE_FOR_CODE, noticeText } = require("../relay-crew-notice");

test("each renewal stop code with a notice maps to its kind", () => {
  assert.deepEqual({ ...NOTICE_FOR_CODE }, {
    CREW_OFFLINE: NOTICE.UNAVAILABLE,
    CREW_ACCESS_NOT_GRANTED: NOTICE.NO_ACCESS,
    REMOTE_ACCESS_DISABLED: NOTICE.ACCESS_OFF,
  });
});

test("the access-off notice names the Crew and offers Check again", () => {
  assert.deepEqual(noticeText(NOTICE.ACCESS_OFF, "Team Crew"), {
    title: "Team Crew's owner turned off access",
    body: "You can open it again when they turn it back on.",
    check: "Check again",
    close: "Close window",
  });
  assert.equal(noticeText(NOTICE.ACCESS_OFF, null).title, "This Crew's owner turned off access");
});
