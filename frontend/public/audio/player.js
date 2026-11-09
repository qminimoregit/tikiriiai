// Gapless playback of streamed PCM16 chunks (24 kHz from Gemini Live), with instant flush on barge-in.
class PcmPlayer {
  constructor(sampleRate = 24000) {
    this.ctx = new AudioContext();
    this.rate = sampleRate;
    this.next = 0;
    this.sources = new Set();
  }

  play(arrayBuffer) {
    const bytes = arrayBuffer.byteLength - (arrayBuffer.byteLength % 2);
    if (!bytes) return;
    const pcm = new Int16Array(arrayBuffer, 0, bytes / 2);
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;

    const buffer = this.ctx.createBuffer(1, floats.length, this.rate);
    buffer.copyToChannel(floats, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const start = Math.max(this.next, this.ctx.currentTime + 0.05);
    src.start(start);
    this.next = start + buffer.duration;
    this.sources.add(src);
    src.onended = () => this.sources.delete(src);
  }

  clear() {
    for (const s of this.sources) {
      try { s.stop(); } catch {}
    }
    this.sources.clear();
    this.next = 0;
  }

  remainingSeconds() {
    return Math.max(0, this.next - this.ctx.currentTime);
  }

  close() {
    this.clear();
    this.ctx.close();
  }
}
