/**
 * Turns the microphone into 16-bit little-endian mono frames of about 100 ms at the AudioContext's
 * real sample rate. The audio thread delivers 128-sample quanta; sending each one as its own socket
 * message would add needless per-message work, so they are merged here.
 *
 * Messages to the page:   { type: 'frame', pcm: ArrayBuffer, samples }
 *                         { type: 'stopped', totalSamples }   after the last (partial) frame
 * Message from the page:  { type: 'stop' }
 */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSamples = Math.max(128, Math.round(sampleRate / 10));
    this.frame = new Int16Array(this.frameSamples);
    this.filled = 0;
    this.total = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop' && !this.stopped) {
        this.stopped = true;
        this.flush();
        this.port.postMessage({ type: 'stopped', totalSamples: this.total });
      }
    };
  }

  flush() {
    if (this.filled === 0) return;
    const pcm = this.frame.slice(0, this.filled);
    this.total += this.filled;
    this.port.postMessage({ type: 'frame', pcm: pcm.buffer, samples: this.filled }, [pcm.buffer]);
    this.filled = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        const s = Math.max(-1, Math.min(1, channel[i]));
        this.frame[this.filled++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.filled === this.frameSamples) this.flush();
      }
    }
    return true;
  }
}

registerProcessor('myvocal-capture', CaptureProcessor);
