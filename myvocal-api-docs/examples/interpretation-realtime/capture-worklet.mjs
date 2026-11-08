class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(1600);
    this.length = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => {
      if (data !== "flush" || this.stopped) return;
      this.stopped = true;
      if (this.length) this.send();
      this.port.postMessage("flushed");
    };
  }

  send() {
    const pcm = new ArrayBuffer(this.length * 2);
    const view = new DataView(pcm);
    for (let i = 0; i < this.length; i++) view.setInt16(i * 2, this.frame[i], true);
    this.port.postMessage(pcm, [pcm]);
    this.length = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      const normalized = Math.max(-1, Math.min(1, sample));
      this.frame[this.length++] = normalized * (normalized < 0 ? 32768 : 32767);
      if (this.length === this.frame.length) this.send();
    }
    return true;
  }
}

registerProcessor("rtt-pcm-capture", PcmCapture);
