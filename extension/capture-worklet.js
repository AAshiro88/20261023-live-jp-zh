// AudioWorklet capture tap: forwards raw Float32 channel buffers to the host
// document (offscreen.js) via port messages. Runs in the audio render thread.
class TapProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0 && input[0] && input[0].length > 0) {
      // copy() needed: the underlying arrays are reused by the render thread
      const chans = input.map((ch) => new Float32Array(ch));
      this.port.postMessage({ chans });
    }
    return true;
  }
}
registerProcessor('tap-processor', TapProcessor);
