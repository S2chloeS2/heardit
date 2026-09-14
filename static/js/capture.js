/* Audio capture for Heardit.
 *
 * Three sources, one interface:
 *   system — what is playing on this computer, via a shared tab
 *   mic    — the room, via the microphone
 *   both   — the two mixed into one track
 *
 * Clips are cut every CLIP_MS by stopping and restarting the recorder, so each
 * one carries its own container header and the server can decode it alone.
 */

const CLIP_MS = 6000;

class Capture {
  constructor({ onClip, onStatus, onLevel }) {
    this.onClip = onClip;
    this.onStatus = onStatus || (() => {});
    this.onLevel = onLevel || (() => {});
    this.recorder = null;
    this.streams = [];
    this.audioContext = null;
    this.analyser = null;
    this.timer = null;
    this.rafId = null;
    this.recording = false;
  }

  get isRecording() {
    return this.recording;
  }

  /** Ask for whichever tracks the chosen source needs. */
  async openStream(source, deviceId) {
    this.streams = [];

    const wantsSystem = source === 'system' || source === 'both';
    const wantsMic = source === 'mic' || source === 'both';

    let systemStream = null;
    if (wantsSystem) {
      if (!navigator.mediaDevices.getDisplayMedia) {
        throw new Error('이 브라우저는 화면 소리 공유를 지원하지 않습니다. 크롬을 써주세요.');
      }
      systemStream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      if (systemStream.getAudioTracks().length === 0) {
        systemStream.getTracks().forEach((t) => t.stop());
        throw new Error(
          '오디오 없이 화면만 공유되었습니다. 공유 창에서 "Chrome 탭"을 고르고 "탭 오디오도 공유"를 켜주세요.'
        );
      }
      // The video track exists only because getDisplayMedia requires it.
      systemStream.getVideoTracks().forEach((t) => t.stop());
      this.streams.push(systemStream);
    }

    let micStream = null;
    if (wantsMic) {
      const constraints = deviceId ? { deviceId: { exact: deviceId } } : true;
      micStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
      this.streams.push(micStream);
    }

    return this.mix(systemStream, micStream);
  }

  /** Combine whichever tracks we got into a single recordable stream. */
  mix(systemStream, micStream) {
    this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const destination = this.audioContext.createMediaStreamDestination();
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = 2048;

    [systemStream, micStream].filter(Boolean).forEach((stream) => {
      const node = this.audioContext.createMediaStreamSource(stream);
      node.connect(destination);
      node.connect(this.analyser);
    });

    return destination.stream;
  }

  async start(source, deviceId) {
    if (this.recording) return;
    const stream = await this.openStream(source, deviceId);

    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
      .find((t) => MediaRecorder.isTypeSupported(t)) || '';

    const makeRecorder = () => {
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      recorder.addEventListener('dataavailable', (event) => {
        if (event.data && event.data.size > 1024) this.onClip(event.data);
      });
      return recorder;
    };

    this.recorder = makeRecorder();
    this.recorder.start();
    this.recording = true;
    this.onStatus('recording');
    this.drawLevels();

    // Cut a clip on an interval. Restarting is what makes each clip decodable.
    this.timer = setInterval(() => {
      if (!this.recorder || this.recorder.state !== 'recording') return;
      this.recorder.stop();
      this.recorder = makeRecorder();
      this.recorder.start();
    }, CLIP_MS);

    // If the user ends the tab share from Chrome's own bar, stop cleanly.
    this.streams.forEach((s) =>
      s.getTracks().forEach((track) => {
        track.addEventListener('ended', () => this.stop());
      })
    );
  }

  stop() {
    if (!this.recording) return;
    this.recording = false;
    clearInterval(this.timer);
    cancelAnimationFrame(this.rafId);

    if (this.recorder && this.recorder.state === 'recording') this.recorder.stop();
    this.recorder = null;

    this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    this.streams = [];

    if (this.audioContext) {
      this.audioContext.close().catch(() => {});
      this.audioContext = null;
    }
    this.analyser = null;
    this.onStatus('stopped');
    this.onLevel(null);
  }

  drawLevels() {
    if (!this.analyser) return;
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (!this.analyser) return;
      this.analyser.getByteTimeDomainData(data);
      this.onLevel(data);
      this.rafId = requestAnimationFrame(tick);
    };
    tick();
  }
}

/** List the microphones available, for the BlackHole / external-device case. */
async function listAudioInputs() {
  try {
    // Labels stay blank until the user has granted mic access once.
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((d) => d.kind === 'audioinput');
  } catch {
    return [];
  }
}

/** Paint the waveform into a canvas. */
function makeWaveformPainter(canvas) {
  const ctx = canvas.getContext('2d');
  const resize = () => {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = canvas.offsetWidth * ratio;
    canvas.height = canvas.offsetHeight * ratio;
    ctx.scale(ratio, ratio);
  };
  resize();
  window.addEventListener('resize', resize);

  const accent = () =>
    getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#6B4BE8';

  return (data) => {
    const w = canvas.offsetWidth;
    const h = canvas.offsetHeight;
    ctx.clearRect(0, 0, w, h);
    if (!data) return;

    ctx.lineWidth = 1.8;
    ctx.strokeStyle = accent();
    ctx.beginPath();
    const step = w / data.length;
    for (let i = 0; i < data.length; i += 1) {
      const y = (data[i] / 128.0) * (h / 2);
      const x = i * step;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  };
}
