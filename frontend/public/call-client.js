// Browser call client shared by the admin Test Call page and the public "Talk to us" page.
// Mic (AudioWorklet, 16 kHz PCM16) -> WebSocket -> server -> Gemini Live; agent audio (24 kHz PCM16) -> PcmPlayer.
// Needs /public/audio/player.js loaded first.

class CallClient {
  /**
   * @param {object} opts
   * @param {string} opts.wsPath      e.g. "/ws/test-call"
   * @param {string} [opts.base]      origin of the server (default: this page's origin)
   * @param {(ev: object) => void} [opts.onEvent]   server events: started, partial, transcript, tool, error, ended
   * @param {(text: string, live: boolean) => void} [opts.onStatus]
   * @param {(info: {status: string, reason: string}) => void} [opts.onFinish]  called exactly once per call
   */
  constructor({ wsPath, base = location.origin, onEvent = () => {}, onStatus = () => {}, onFinish = () => {} }) {
    this.wsUrl = base.replace(/^http/, "ws") + wsPath;
    this.workletUrl = new URL("/public/audio/mic-worklet.js", base).href;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.onFinish = onFinish;
    this.ws = this.player = this.micCtx = this.micStream = null;
    this.finished = true;
  }

  // Browsers only expose the microphone on secure pages: https://, http://localhost or http://127.0.0.1.
  static micUnavailableReason() {
    if (navigator.mediaDevices?.getUserMedia) return "";
    return window.isSecureContext
      ? "This browser doesn't support microphone access. Please use Chrome, Edge, Safari or Firefox."
      : `The browser blocks the microphone on ${location.host}. Open this page over https (or on localhost).`;
  }

  async start() {
    const blocked = CallClient.micUnavailableReason();
    if (blocked) throw new Error(blocked);
    this.finished = false;
    this.endInfo = null;
    // The playback context must be created inside the click handler, or browsers keep it muted.
    this.player = new PcmPlayer(24000);
    this.onStatus("Waiting for microphone permission…", false);
    try {
      await this._startMic();
    } catch (err) {
      this._cleanup();
      this.finished = true;
      throw new Error(
        err.name === "NotAllowedError" ? "Microphone permission was denied. Allow it from the icon in the address bar, then try again."
          : err.name === "NotFoundError" ? "No microphone was found. Plug one in and try again."
          : "Could not open the microphone: " + err.message
      );
    }
    this.onStatus("Connecting…", false);
    this.ws = new WebSocket(this.wsUrl);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (e) => {
      if (typeof e.data !== "string") return this.player?.play(e.data);
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (ev.type === "interrupted") this.player?.clear();
      if (ev.type === "started") this.onStatus("Connected — the agent is answering", true);
      if (ev.type === "ended") this.endInfo = ev;
      this.onEvent(ev);
      if (ev.type === "ended") this._finish(ev);
    };
    this.ws.onclose = (e) => {
      const reason = e.code === 4401 ? "Please sign in again" : e.code === 4403 ? "Calls are not available here" : "Disconnected";
      this._finish(this.endInfo || { status: e.code >= 4400 ? "failed" : "completed", reason });
    };
    this.ws.onerror = () => this.onStatus("Connection problem", false);
  }

  // A key press (for the language menu at the start of the call).
  sendDigit(digit) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "dtmf", digit }));
  }

  hangup() {
    if (this.finished) return;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "hangup" }));
    this.player?.clear();
    this._stopMic();
    this.onStatus("Hanging up…", false);
    // The server answers with "ended"; if it never does, don't leave the page hanging.
    clearTimeout(this._hangupTimer);
    this._hangupTimer = setTimeout(() => this._finish({ status: "completed", reason: "caller_hung_up" }), 5000);
  }

  async _startMic() {
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.micCtx = new AudioContext();
    await this.micCtx.audioWorklet.addModule(this.workletUrl);
    const source = this.micCtx.createMediaStreamSource(this.micStream);
    const node = new AudioWorkletNode(this.micCtx, "mic-processor");
    node.port.onmessage = (e) => {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(e.data);
    };
    const mute = this.micCtx.createGain();
    mute.gain.value = 0; // keeps the graph running without playing the mic back
    source.connect(node).connect(mute).connect(this.micCtx.destination);
  }

  _stopMic() {
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.micCtx?.close().catch(() => {});
    this.micStream = this.micCtx = null;
  }

  _finish(info) {
    if (this.finished) return;
    this.finished = true;
    clearTimeout(this._hangupTimer);
    this._stopMic();
    // Let the goodbye finish playing before closing everything.
    const wait = this.player ? this.player.remainingSeconds() * 1000 : 0;
    setTimeout(() => {
      this._cleanup();
      this.onFinish(info);
    }, wait);
  }

  _cleanup() {
    this._stopMic();
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) this.ws.close();
    this.ws = null;
    this.player?.close();
    this.player = null;
  }
}

// Live transcript view: partial bubbles that turn final when a turn completes.
class TranscriptView {
  constructor(container, labels = { agent: "Agent", caller: "You" }) {
    this.el = container;
    this.labels = labels;
    this.live = {};
  }

  reset(placeholder = "") {
    this.el.replaceChildren();
    this.live = {};
    if (placeholder) {
      const p = document.createElement("div");
      p.className = "empty";
      p.textContent = placeholder;
      this.el.append(p);
    }
  }

  _bubble(role) {
    this.el.querySelector(".empty")?.remove();
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (role !== "tool" && role !== "system") {
      const who = document.createElement("div");
      who.className = "who";
      who.textContent = this.labels[role] || role;
      div.append(who);
    }
    const body = document.createElement("span");
    div.append(body);
    this.el.append(div);
    return div;
  }

  _scroll() {
    this.el.scrollTop = this.el.scrollHeight;
  }

  partial(role, text) {
    const div = this.live[role] || (this.live[role] = this._bubble(role));
    div.classList.add("partial");
    div.lastChild.textContent = text;
    this._scroll();
  }

  final(role, text, interrupted = false) {
    const div = this.live[role] || this._bubble(role);
    delete this.live[role];
    div.classList.remove("partial");
    div.lastChild.textContent = text;
    if (interrupted) {
      const cut = document.createElement("span");
      cut.className = "cut";
      cut.textContent = "(interrupted)";
      div.append(cut);
    }
    this._scroll();
  }

  system(text) {
    this.final("tool", text);
  }

  handle(ev) {
    if (ev.type === "partial") this.partial(ev.role, ev.text);
    else if (ev.type === "transcript") ev.role === "tool" || ev.role === "menu" || ev.role === "note" ? this.system(ev.text) : this.final(ev.role, ev.text, ev.interrupted);
    else if (ev.type === "error") this.system(`Error: ${ev.message}`);
  }
}
