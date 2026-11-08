async function broker(path, body) {
  const response = await fetch("/sample/" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.code);
  return value;
}

function bytesToBase64(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)));
}

function playPcm(context, event, nextTime) {
  const raw = Uint8Array.from(atob(event.data), byte => byte.charCodeAt(0));
  const view = new DataView(raw.buffer);
  const audio = context.createBuffer(1, raw.length / 2, event.sampleRate);
  const samples = audio.getChannelData(0);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  const node = context.createBufferSource();
  node.buffer = audio;
  node.connect(context.destination);
  const at = Math.max(context.currentTime + 0.02, nextTime);
  node.start(at);
  return at + audio.duration;
}

export async function startRound({ source = "microphone", options, onEvent = () => {} }) {
  // Invoke from an existing button's click handler so capture and playback have user activation.
  const context = new AudioContext({ sampleRate: 16000 });
  const resumed = context.resume();
  const mediaRequest = source === "tab"
    ? navigator.mediaDevices.getDisplayMedia({ audio: true, video: true })
    : navigator.mediaDevices.getUserMedia({ audio: true });
  let media, remote, socket, worklet;
  let sequence = 0, nextTime = 0, stopping = false, final = false;
  let tracks = [];
  const stopTracks = () => tracks.forEach(track => track.stop());
  const endRemote = () => broker("end", { sessionId: remote.sessionId });

  try {
    media = await mediaRequest;
    tracks = media.getTracks();
    await resumed;
    if (!media.getAudioTracks().length || context.sampleRate !== 16000) throw new Error("RTT_SAMPLE_CAPTURE_UNAVAILABLE");
    await context.audioWorklet.addModule(new URL("./capture-worklet.mjs", import.meta.url));
    remote = await broker("round", { sessionKey: crypto.randomUUID(), roundKey: crypto.randomUUID(), options });
    socket = new WebSocket(remote.url, ["myvocal.rtt.v1", "ticket." + remote.ticket]);
    const completed = new Promise(resolve => {
      const finish = async () => {
        stopTracks();
        if (!final) {
          try {
            await endRemote();
            const state = await broker("status", { sessionId: remote.sessionId });
            onEvent({ type: "snapshot", data: state });
          } catch {
            onEvent({ type: "query_required", sessionId: remote.sessionId, roundId: remote.roundId });
          }
        } else await endRemote().catch(() => {});
        await new Promise(resolve => setTimeout(resolve, Math.max(0, nextTime - context.currentTime) * 1000));
        await context.close();
        resolve();
      };
      socket.addEventListener("message", ({ data }) => {
        const event = JSON.parse(data);
        if (event.type === "ready" && !stopping) {
          const input = context.createMediaStreamSource(media);
          worklet = new AudioWorkletNode(context, "rtt-pcm-capture", { channelCount: 1 });
          const silent = context.createGain();
          silent.gain.value = 0;
          input.connect(worklet).connect(silent).connect(context.destination);
          worklet.port.onmessage = ({ data }) => {
            if (socket.readyState !== WebSocket.OPEN) return;
            if (data === "flushed") socket.send(JSON.stringify({ type: "finish" }));
            else if (socket.bufferedAmount <= 512 * 1024) {
              socket.send(JSON.stringify({ type: "audio", sequence: sequence++, data: bytesToBase64(data) }));
            } else {
              onEvent({ type: "client_error", code: "RTT_SAMPLE_BACKPRESSURE" });
              stop();
            }
          };
        }
        if (event.type === "audio") nextTime = playPcm(context, event, nextTime);
        if (event.type === "input_ended") { stopping = true; stopTracks(); }
        if (event.type === "done") { final = true; stopTracks(); }
        onEvent(event);
      });
      socket.addEventListener("close", finish, { once: true });
    });

    function stop() {
      if (stopping) return;
      stopping = true;
      if (worklet) worklet.port.postMessage("flush");
      else if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "finish" }));
      else socket.close();
      stopTracks();
    }

    media.getAudioTracks().forEach(track => track.addEventListener("ended", stop, { once: true }));
    return { sessionId: remote.sessionId, roundId: remote.roundId, completed, stop };
  } catch (failure) {
    stopTracks();
    if (socket) socket.close();
    if (remote) await endRemote().catch(() => {});
    await context.close();
    throw failure;
  }
}

export function querySession(sessionId) { return broker("status", { sessionId }); }
