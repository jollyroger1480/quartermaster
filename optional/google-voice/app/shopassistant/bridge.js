// Shop Assistant audio bridge — injected into every Google Voice frame before
// page scripts run. Replaces the microphone with a stream the bot writes into,
// and taps the caller's WebRTC audio so the bot can hear it. No virtual audio
// cables, no speakers, no mic: PCM crosses to Python over CDP bindings
// (qmAudio / qmEvent), which also sidesteps page CSP and local-network rules.
(() => {
  if (window.__qm) return;
  const RATE = 48000;      // AudioContext rate (fixed so decimation is exact)
  const TARGET = 16000;    // what whisper wants
  const S = {
    ctx: null, dest: null, player: null, queue: [], qpos: 0, queued: 0,
    cap: null, capSrc: null, capTrack: null, keep: null,
    pcs: new Set(), attached: new WeakSet(),
    muteSpeakers: true, fakeMic: true, frames: 0,
  };

  const call = (name, arg) => {
    try { const f = window[name]; if (typeof f === 'function') f(arg); } catch (e) {}
  };
  const ev = (type, data) =>
    call('qmEvent', JSON.stringify(Object.assign({ type, t: Date.now(), href: location.href }, data || {})));

  function b64ToI16(b64) {
    const bin = atob(b64);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Int16Array(u8.buffer);
  }
  function i16ToB64(i16) {
    const u8 = new Uint8Array(i16.buffer);
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  }

  function ctx() {
    if (!S.ctx) {
      S.ctx = new AudioContext({ sampleRate: RATE, latencyHint: 'interactive' });
      S.dest = S.ctx.createMediaStreamDestination();
      // playback: a script node pulls queued bot audio into the fake mic
      S.player = S.ctx.createScriptProcessor(2048, 1, 1);
      const src = S.ctx.createConstantSource();
      src.offset.value = 0;
      src.connect(S.player);
      src.start();
      S.player.onaudioprocess = (e) => {
        const out = e.outputBuffer.getChannelData(0);
        let i = 0;
        while (i < out.length && S.queue.length) {
          const buf = S.queue[0];
          const n = Math.min(out.length - i, buf.length - S.qpos);
          out.set(buf.subarray(S.qpos, S.qpos + n), i);
          i += n; S.qpos += n; S.queued -= n;
          if (S.qpos >= buf.length) { S.queue.shift(); S.qpos = 0; }
        }
        if (i < out.length) out.fill(0, i);
      };
      S.player.connect(S.dest);
      const sink = S.ctx.createGain();       // keeps the node pulled, inaudible
      sink.gain.value = 0;
      S.player.connect(sink);
      sink.connect(S.ctx.destination);
    }
    if (S.ctx.state !== 'running') S.ctx.resume().catch(() => {});
    return S.ctx;
  }

  function attach(track) {
    if (!track || track.kind !== 'audio' || S.attached.has(track)) return;
    S.attached.add(track);
    const c = ctx();
    const stream = new MediaStream([track]);
    // Chrome only feeds remote WebRTC audio into WebAudio when the stream is
    // also attached to a media element — a muted one is enough.
    try { const a = new Audio(); a.muted = true; a.srcObject = stream; a.play().catch(() => {}); S.keep = a; } catch (e) {}
    if (S.capSrc) { try { S.capSrc.disconnect(); } catch (e) {} }
    S.capSrc = c.createMediaStreamSource(stream);
    if (!S.cap) {
      S.cap = c.createScriptProcessor(2048, 1, 1);
      S.cap.onaudioprocess = (e) => {
        if (!S.capTrack || S.capTrack.readyState !== 'live') return;
        const inp = e.inputBuffer.getChannelData(0);
        const step = RATE / TARGET;
        const n = Math.floor(inp.length / step);
        const o = new Int16Array(n);
        for (let i = 0; i < n; i++) {
          const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
          let s = 0;
          for (let k = a; k < b; k++) s += inp[k];
          const v = Math.max(-1, Math.min(1, s / (b - a)));
          o[i] = v * 32767;
        }
        S.frames++;
        call('qmAudio', i16ToB64(o));
      };
      const z = c.createGain();
      z.gain.value = 0;
      S.cap.connect(z);
      z.connect(c.destination);
    }
    S.capSrc.connect(S.cap);
    S.capTrack = track;
    track.addEventListener('ended', () => ev('track-ended', { id: track.id }));
    ev('remote-track', { id: track.id });
  }

  // ---- microphone: hand Google Voice our synthetic track -------------------
  try {
    const MD = window.MediaDevices && MediaDevices.prototype;
    if (MD && MD.getUserMedia) {
      const orig = MD.getUserMedia;
      MD.getUserMedia = async function (c) {
        if (!c || !c.audio || !S.fakeMic) return orig.call(this, c);
        ctx();
        const ms = new MediaStream([S.dest.stream.getAudioTracks()[0].clone()]);
        if (c.video) {
          try { const v = await orig.call(this, { video: c.video }); v.getVideoTracks().forEach((t) => ms.addTrack(t)); } catch (e) {}
        }
        ev('gum', {});
        return ms;
      };
      const origEnum = MD.enumerateDevices;
      MD.enumerateDevices = async function () {
        const list = await origEnum.call(this);
        if (S.fakeMic && !list.some((d) => d.kind === 'audioinput')) {
          const fake = { deviceId: 'default', kind: 'audioinput', label: 'Shop Assistant', groupId: 'qm' };
          fake.toJSON = () => ({ ...fake });
          return list.concat([fake]);
        }
        return list;
      };
    }
  } catch (e) {}

  // ---- peer connections: tap the caller's audio, report call state --------
  function hookPC(pc) {
    S.pcs.add(pc);
    ev('pc-new', {});
    pc.addEventListener('track', (e) => attach(e.track));
    const st = () => {
      ev('pc-state', { state: pc.connectionState, ice: pc.iceConnectionState });
      if (pc.connectionState === 'closed') S.pcs.delete(pc);
    };
    pc.addEventListener('connectionstatechange', st);
    pc.addEventListener('iceconnectionstatechange', st);
    const oc = pc.close.bind(pc);
    pc.close = () => { try { oc(); } finally { S.pcs.delete(pc); ev('pc-state', { state: 'closed' }); } };
  }
  try {
    const NPC = window.RTCPeerConnection;
    if (NPC) {
      class QPC extends NPC { constructor(...a) { super(...a); try { hookPC(this); } catch (e) {} } }
      window.RTCPeerConnection = QPC;
      if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = QPC;
    }
  } catch (e) {}
  // fallback for pages that wire audio without firing 'track' listeners we see
  setInterval(() => {
    for (const pc of S.pcs) {
      try {
        if (pc.connectionState !== 'connected') continue;
        for (const r of pc.getReceivers()) {
          if (r.track && r.track.kind === 'audio' && r.track.readyState === 'live' && !S.attached.has(r.track)) attach(r.track);
        }
      } catch (e) {}
    }
  }, 1000);

  // ---- keep the caller off the PC speakers (optional) ---------------------
  try {
    const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
    Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
      configurable: true, enumerable: d.enumerable,
      get() { return d.get.call(this); },
      set(v) {
        d.set.call(this, v);
        try {
          if (S.muteSpeakers && this !== S.keep && v && v.getAudioTracks && v.getAudioTracks().length) this.muted = true;
        } catch (e) {}
      },
    });
  } catch (e) {}

  // ---- API used by Python via page.evaluate -------------------------------
  window.__qm = {
    version: 1,
    play(b64, rate) {
      ctx();
      const src = b64ToI16(b64);
      const ratio = RATE / rate;
      const n = Math.floor(src.length * ratio);
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const x = i / ratio, j = Math.floor(x), f = x - j;
        const a = src[j] || 0, b = j + 1 < src.length ? src[j + 1] : a;
        out[i] = (a + (b - a) * f) / 32768;
      }
      S.queue.push(out);
      S.queued += n;
      return S.queued / RATE;
    },
    flush() { S.queue = []; S.qpos = 0; S.queued = 0; return 0; },
    dtmf(tones) {
      let n = 0;
      for (const pc of S.pcs) {
        try {
          for (const snd of pc.getSenders()) {
            if (snd.track && snd.track.kind === 'audio' && snd.dtmf && snd.dtmf.canInsertDTMF) {
              snd.dtmf.insertDTMF(tones, 150, 80); n++;
            }
          }
        } catch (e) {}
      }
      return n;
    },
    queued() { return S.queued / RATE; },
    config(o) { Object.assign(S, o || {}); return true; },
    status() {
      return {
        ctx: S.ctx ? S.ctx.state : 'none',
        pcs: [...S.pcs].map((p) => p.connectionState),
        capLive: !!(S.capTrack && S.capTrack.readyState === 'live'),
        queued: S.queued / RATE, frames: S.frames, href: location.href,
      };
    },
  };
  ev('bridge-ready', {});
})();
