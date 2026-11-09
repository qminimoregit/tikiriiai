// AudioWorklet: capture mic audio, downsample to 16 kHz PCM16, post ~40 ms chunks.
const TARGET_RATE = 16000;
const CHUNK = 640; // 40 ms at 16 kHz

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // `sampleRate` is the context rate (global in worklet scope)
    this.phase = 0;
    this.acc = 0;
    this.count = 0;
    this.out = new Int16Array(CHUNK);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      // Box-filter downsampling: average the input samples that fall into each output sample.
      this.acc += ch[i];
      this.count++;
      this.phase += 1;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.acc / this.count));
        this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        this.acc = 0;
        this.count = 0;
        if (this.n === CHUNK) {
          this.port.postMessage(this.out.buffer.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
