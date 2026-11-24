/* xr-emu glue: runs right after IWER's UMD build (which defines window.IWER).
 * Creates an emulated Meta Quest 3 (immersive-ar + immersive-vr + hand-tracking),
 * installs the runtime over navigator.xr, and exposes window.__xrEmu.
 * Every function takes/returns plain JSON so page.evaluate can call it.
 * Coordinates are IWER GlobalSpace, which is exactly the 'local-floor'
 * reference space (y = 0 is the floor, -Z is "ahead" at yaw 0). */
(function () {
  'use strict';
  var IWER = window.IWER;
  if (!IWER || !IWER.XRDevice) {
    throw new Error('xr-emu: IWER UMD build did not load');
  }
  var device = new IWER.XRDevice(IWER.metaQuest3);
  // forceInstall: headless Chromium may expose a native navigator.xr stub.
  device.installRuntime({ forceInstall: true });

  var D2R = Math.PI / 180;
  var lastSession = null;
  var events = [];

  // Track the active session so end() and frames() work and 'end' is logged.
  var xr = navigator.xr;
  var realRequest = xr.requestSession.bind(xr);
  xr.requestSession = function (mode, init) {
    return realRequest(mode, init).then(function (s) {
      lastSession = s;
      events.push({ type: 'session-start', mode: mode });
      s.addEventListener('end', function () { events.push({ type: 'session-end' }); });
      return s;
    });
  };

  function hand(name) {
    if (name !== 'left' && name !== 'right') throw new Error('hand must be "left" or "right"');
    return name;
  }
  function norm(v) {
    var l = Math.hypot(v[0], v[1], v[2]);
    if (!(l > 1e-9)) throw new Error('zero-length vector');
    return [v[0] / l, v[1] / l, v[2] / l];
  }
  // Quaternion (x,y,z,w) from yaw (about +Y) then pitch (about local +X).
  function yawPitchQuat(yaw, pitch) {
    var cy = Math.cos(yaw / 2), sy = Math.sin(yaw / 2);
    var cp = Math.cos(pitch / 2), sp = Math.sin(pitch / 2);
    // qYaw * qPitch
    return [cy * sp, sy * cp, -sy * sp, cy * cp];
  }
  // Rotation taking local -Z onto dir, with no roll (local +X stays horizontal).
  function quatForDirection(dir) {
    var d = norm(dir);
    var yaw = Math.atan2(-d[0], -d[2]);
    var pitch = Math.asin(Math.max(-1, Math.min(1, d[1])));
    return yawPitchQuat(yaw, pitch);
  }
  // Quaternion from an orthonormal basis given as columns x, y, z.
  function quatFromBasis(x, y, z) {
    var m00 = x[0], m01 = y[0], m02 = z[0];
    var m10 = x[1], m11 = y[1], m12 = z[1];
    var m20 = x[2], m21 = y[2], m22 = z[2];
    var t = m00 + m11 + m22, qx, qy, qz, qw, s;
    if (t > 0) { s = Math.sqrt(t + 1) * 2; qw = s / 4; qx = (m21 - m12) / s; qy = (m02 - m20) / s; qz = (m10 - m01) / s; }
    else if (m00 > m11 && m00 > m22) { s = Math.sqrt(1 + m00 - m11 - m22) * 2; qw = (m21 - m12) / s; qx = s / 4; qy = (m01 + m10) / s; qz = (m02 + m20) / s; }
    else if (m11 > m22) { s = Math.sqrt(1 + m11 - m00 - m22) * 2; qw = (m02 - m20) / s; qx = (m01 + m10) / s; qy = s / 4; qz = (m12 + m21) / s; }
    else { s = Math.sqrt(1 + m22 - m00 - m11) * 2; qw = (m10 - m01) / s; qx = (m02 + m20) / s; qy = (m12 + m21) / s; qz = s / 4; }
    var l = Math.hypot(qx, qy, qz, qw);
    return [qx / l, qy / l, qz / l, qw / l];
  }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  // IWER's wrist convention (see its wristRotationFromDirections): wrist -Z =
  // fingers, wrist -Y = palm normal. Unlike IWER's helper, the PALM is kept
  // exact and the finger direction is bent to be perpendicular to it.
  function wristQuat(fingers, palm) {
    var y = norm([-palm[0], -palm[1], -palm[2]]);
    var z = [-fingers[0], -fingers[1], -fingers[2]];
    var k = dot(z, y);
    z = norm([z[0] - y[0] * k, z[1] - y[1] * k, z[2] - y[2] * k]);
    var x = cross(y, z);
    return quatFromBasis(x, y, z);
  }
  function vec(v) { return { x: v.x, y: v.y, z: v.z }; }
  function quatObj(q) { return { x: q.x, y: q.y, z: q.z, w: q.w }; }
  function inputsFor(name) {
    return [device.controllers[name], device.hands[name]].filter(Boolean);
  }
  function primary(name) {
    var inp = device.primaryInputMode === 'hand' ? device.hands[name] : device.controllers[name];
    if (!inp) throw new Error('no ' + device.primaryInputMode + ' for ' + name);
    return inp;
  }

  var api = {
    version: device.version,
    info: function () {
      var s = device.activeSession;
      return {
        device: device.name,
        iwerVersion: device.version,
        modes: device.supportedSessionModes.slice(),
        features: device.supportedFeatures.slice(),
        inputMode: device.primaryInputMode,
        sessionActive: !!s,
        head: { position: vec(device.position), quaternion: quatObj(device.quaternion) },
        inputSources: device.inputSources.map(function (src) {
          return { handedness: src.handedness, targetRayMode: src.targetRayMode, hand: !!src.hand, profiles: Array.prototype.slice.call(src.profiles) };
        }),
        controllers: ['left', 'right'].map(function (h) {
          var c = device.controllers[h];
          return c ? { hand: h, connected: c.connected, position: vec(c.position), quaternion: quatObj(c.quaternion) } : null;
        }),
        hands: ['left', 'right'].map(function (h) {
          var c = device.hands[h];
          return c ? { hand: h, connected: c.connected, position: vec(c.position), quaternion: quatObj(c.quaternion), pinch: c.pinchValue } : null;
        }),
        events: events.slice()
      };
    },
    // yawDeg > 0 turns the head to the left (counter-clockwise seen from above);
    // pitchDeg > 0 looks up. At 0/0 the head looks along -Z.
    setHead: function (o) {
      o = o || {};
      if (o.position) device.position.set(o.position[0], o.position[1], o.position[2]);
      var q = yawPitchQuat((o.yawDeg || 0) * D2R, (o.pitchDeg || 0) * D2R);
      device.quaternion.set(q[0], q[1], q[2], q[3]);
      return api.info().head;
    },
    setInputMode: function (mode) {
      if (mode !== 'controller' && mode !== 'hand') throw new Error('mode must be controller|hand');
      device.primaryInputMode = mode;
      return mode;
    },
    // The input's pose IS its targetRaySpace: position = ray origin, and the
    // ray points along the pose's local -Z. Applies to whichever input
    // (controller or hand) is primary now; for a hand it also releases any
    // setHandPose pin, and the joints hang off this pose.
    setRay: function (o) {
      var inp = primary(hand(o.hand));
      if (inp.setPin) inp.setPin(null);
      var q = quatForDirection(o.direction);
      // rollDeg > 0 rolls the hand to the right about the way it points (a wrist turn), on top of the aim.
      if (o.rollDeg) {
        var h = -o.rollDeg * D2R / 2, sz = Math.sin(h), cz = Math.cos(h);
        q = [q[0] * cz + q[1] * sz, q[1] * cz - q[0] * sz, q[2] * cz + q[3] * sz, q[3] * cz - q[2] * sz];
      }
      inp.position.set(o.origin[0], o.origin[1], o.origin[2]);
      inp.quaternion.set(q[0], q[1], q[2], q[3]);
      return { position: vec(inp.position), quaternion: quatObj(inp.quaternion) };
    },
    // down:true -> selectstart on the next frame; down:false -> select + selectend.
    // Press and release must land in different frames (await frames(2) between).
    select: function (o) {
      var inp = primary(hand(o.hand));
      var v = o.down ? 1 : 0;
      if (device.primaryInputMode === 'hand') inp.updatePinchValue(v);
      else inp.updateButtonValue('trigger', v);
      return v;
    },
    // Ends a held press with selectend only (no select), via IWER's immediate
    // write, which cancels a press the app already saw.
    cancelSelect: function (o) {
      var inp = primary(hand(o.hand));
      if (device.primaryInputMode === 'hand') inp.setPinchValueImmediate(0);
      else inp.setButtonValueImmediate('trigger', 0);
      return true;
    },
    // Pins the hand's wrist joint at position with fingers pointing up and the
    // palm facing the head (or away). Uses IWER XRHandInput.setPin, re-solved
    // every frame until setRay or setHandPose({clear:true}) releases it.
    setHandPose: function (o) {
      var h = device.hands[hand(o.hand)];
      if (o.clear) { h.setPin(null); return null; }
      var p = o.position;
      var head = [device.position.x, device.position.y, device.position.z];
      var toHead = norm([head[0] - p[0], head[1] - p[1], head[2] - p[2]]);
      // `palm` gives the way the palm faces outright (a palm down on a Bot, a palm held up at one).
      var palm = o.palm ? norm(o.palm) : o.palmTowardHead === false ? [-toHead[0], -toHead[1], -toHead[2]] : toHead;
      var q = wristQuat(o.fingers || [0, 1, 0], palm);
      h.setPin({ anchor: 'wrist', target: [p[0], p[1], p[2]], wristRotation: [q[0], q[1], q[2], q[3]] });
      return { position: vec(h.position), quaternion: quatObj(h.quaternion) };
    },
    // Sets the whole-hand shape to one of IWER's built-in poses ('default', 'point',
    // 'pinch', ...). The joints follow at the next frame. Returns the pose now set
    // and the poses that exist.
    setHandShape: function (o) {
      var h = device.hands[hand(o.hand)];
      if (o.pose) h.poseId = o.pose;
      return { pose: h.poseId, poses: Array.prototype.slice.call(h.poseIds || []) };
    },
    // Disconnects both the controller and the hand on that side; the change
    // reaches the app as inputsourceschange on the next frame.
    disconnect: function (o) { inputsFor(hand(o.hand)).forEach(function (i) { i.connected = false; }); return true; },
    reconnect: function (o) { inputsFor(hand(o.hand)).forEach(function (i) { i.connected = true; }); return true; },
    // Resolves after n XR frames of the active session (window rAF otherwise).
    frames: function (n) {
      n = n || 1;
      var s = device.activeSession;
      return new Promise(function (resolve) {
        var left = n;
        function tick() {
          if (--left <= 0) return resolve(n);
          raf(tick);
        }
        function raf(cb) {
          if (s && !s[IWER.P_SESSION].ended) s.requestAnimationFrame(function () { cb(); });
          else window.requestAnimationFrame(function () { cb(); });
        }
        raf(tick);
      });
    },
    end: function () {
      var s = device.activeSession || lastSession;
      if (!s) return false;
      return s.end().then(function () { return true; });
    }
  };
  Object.defineProperty(window, '__xrEmu', { value: api, configurable: false, writable: false });
  Object.defineProperty(window, '__xrEmuDevice', { value: device, configurable: false, writable: false });
})();
