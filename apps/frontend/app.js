const BAND_ORDER = [
  { key: "delta", label: "Delta", range: [1, 4], color: "#f06e59" },
  { key: "theta", label: "Theta", range: [4, 8], color: "#f3b547" },
  { key: "alpha", label: "Alpha", range: [8, 13], color: "#169e92" },
  { key: "beta", label: "Beta", range: [13, 30], color: "#7dd3c7" },
  { key: "gamma", label: "Gamma", range: [30, 45], color: "#c54d74" },
];

const CHANNEL_COLORS = ["#f3b547", "#7dd3c7", "#f06e59", "#f18fbb"];

const elements = {
  connectionCopy: document.getElementById("connection-copy"),
  statusChip: document.getElementById("status-chip"),
  sourceChip: document.getElementById("source-chip"),
  modelChip: document.getElementById("model-chip"),
  versionChip: document.getElementById("version-chip"),
  sampleRate: document.getElementById("sample-rate"),
  batteryValue: document.getElementById("battery-value"),
  batteryBarFill: document.getElementById("battery-bar-fill"),
  batteryStatus: document.getElementById("battery-status"),
  batteryTrend: document.getElementById("battery-trend"),
  batterySource: document.getElementById("battery-source"),
  batteryNote: document.getElementById("battery-note"),
  supportGrid: document.getElementById("support-grid"),
  fitScore: document.getElementById("fit-score"),
  fitNote: document.getElementById("fit-note"),
  fitChannelGrid: document.getElementById("fit-channel-grid"),
  motionScore: document.getElementById("motion-score"),
  motionNote: document.getElementById("motion-note"),
  motionGrid: document.getElementById("motion-grid"),
  calibrationScore: document.getElementById("calibration-score"),
  calibrationNote: document.getElementById("calibration-note"),
  calibrationGuide: document.getElementById("calibration-guide"),
  versionName: document.getElementById("version-name"),
  versionConfidence: document.getElementById("version-confidence"),
  versionIdentity: document.getElementById("version-identity"),
  versionEvidence: document.getElementById("version-evidence"),
  bandsGrid: document.getElementById("bands-grid"),
  momentGrid: document.getElementById("moment-grid"),
  waveformCanvas: document.getElementById("waveform-canvas"),
  trendCanvas: document.getElementById("trend-canvas"),
  bandTrendCanvas: document.getElementById("band-trend-canvas"),
};

const state = {
  snapshot: null,
  source: null,
  bandTrendHistory: [],
};

function start() {
  connectStream();
  window.addEventListener("resize", drawAllCharts);
}

function connectStream() {
  const stream = new EventSource("/api/stream");
  state.source = stream;
  stream.addEventListener("snapshot", (event) => {
    const payload = JSON.parse(event.data);
    render(payload);
  });
  stream.onerror = () => {
    stream.close();
    pollOnce();
    setTimeout(connectStream, 1400);
  };
}

async function pollOnce() {
  const response = await fetch("/api/status");
  if (!response.ok) {
    return;
  }
  render(await response.json());
}

function render(snapshot) {
  state.snapshot = snapshot;
  pushBandTrendPoint(snapshot);
  renderStatus(snapshot);
  renderBattery(snapshot);
  renderSupport(snapshot);
  renderFit(snapshot);
  renderMotion(snapshot);
  renderCalibration(snapshot);
  renderVersion(snapshot);
  renderBands(snapshot);
  renderMoments(snapshot);
  drawAllCharts();
}

function renderStatus(snapshot) {
  const connected = snapshot.connection.connected;
  const versionName = snapshot.device.version.hardwareName;
  elements.connectionCopy.textContent = snapshot.connection.statusLine;
  elements.statusChip.textContent = connected ? "Live MuseLSL" : "Demo feed";
  elements.statusChip.style.background = connected
    ? "linear-gradient(135deg, rgba(15,118,110,0.96), rgba(22,158,146,0.96))"
    : "linear-gradient(135deg, rgba(240,110,89,0.96), rgba(243,181,71,0.96))";
  elements.sourceChip.textContent = `Source: ${snapshot.connection.mode}`;
  elements.modelChip.textContent = `Profile: ${snapshot.device.label}`;
  elements.versionChip.textContent = `Version: ${versionName}`;
  elements.sampleRate.textContent = snapshot.eeg.sampleRateHz;
}

function renderBattery(snapshot) {
  const percent = snapshot.battery.percent;
  elements.batteryValue.textContent = `${percent.toFixed(1)}%`;
  elements.batteryBarFill.style.width = `${Math.max(6, percent)}%`;
  elements.batteryStatus.textContent = toTitleCase(snapshot.battery.level);
  elements.batteryTrend.textContent = toTitleCase(snapshot.battery.trend);
  elements.batterySource.textContent = snapshot.battery.source;
  elements.batteryNote.textContent = snapshot.connection.telemetryAvailable
    ? "Battery telemetry is flowing through MuseLSL."
    : "Telemetry is not live yet, so the dashboard is showing the bridge battery estimate.";
}

function renderSupport(snapshot) {
  elements.supportGrid.innerHTML = snapshot.device.supportedProfiles
    .map((profile) => {
      const active = profile.id === snapshot.device.selectedProfile;
      return `
        <article class="support-card">
          <span class="support-pill">${active ? "Selected" : "Supported"}</span>
          <h3>${profile.label}</h3>
          <p>${profile.headline}</p>
        </article>
      `;
    })
    .join("");
}

function renderFit(snapshot) {
  elements.fitScore.textContent = `${snapshot.sensorFit.overallScore.toFixed(0)}%`;
  elements.fitNote.textContent = `${toTitleCase(snapshot.sensorFit.overallLabel)} fit. ${snapshot.sensorFit.method}`;
  elements.fitChannelGrid.innerHTML = snapshot.sensorFit.channels
    .map((channel) => {
      return `
        <article class="fit-card">
          <span class="fit-pill">${toTitleCase(channel.label)}</span>
          <h3>${channel.channel}</h3>
          <div class="fit-meter-shell"><div class="fit-meter-fill" style="width:${channel.score}%"></div></div>
          <p class="fit-copy">${toTitleCase(channel.contactState)} - spread ${formatValue(channel.signalSpreadUv)} uV, peak-to-peak ${formatValue(channel.peakToPeakUv)} uV.</p>
        </article>
      `;
    })
    .join("");
}

function renderVersion(snapshot) {
  const version = snapshot.device.version;
  elements.versionName.textContent = version.hardwareName;
  elements.versionConfidence.textContent = `Confidence: ${toTitleCase(version.confidence)}`;
  elements.versionIdentity.textContent = `Identity: ${version.streamIdentity || "Unknown stream"}`;
  elements.versionEvidence.innerHTML = version.evidence
    .map((item) => `<li>${item}</li>`)
    .join("");
}

function renderMotion(snapshot) {
  const motion = snapshot.motion;
  elements.motionScore.textContent = `${motion.stabilityScore.toFixed(0)}%`;
  elements.motionNote.textContent = motion.available
    ? `${toTitleCase(motion.stabilityLabel)} stability. Head is ${motion.headPose.tiltLabel}.`
    : "Motion streams are not live yet, so head-position tracking is waiting for ACC/GYRO.";
  elements.motionGrid.innerHTML = `
    <article class="fit-card">
      <span class="fit-pill">Pose</span>
      <h3>Orientation</h3>
      <p class="fit-copy">Pitch ${motion.headPose.pitchDeg.toFixed(1)} deg, roll ${motion.headPose.rollDeg.toFixed(1)} deg.</p>
    </article>
    <article class="fit-card">
      <span class="fit-pill">Motion</span>
      <h3>Stability</h3>
      <p class="fit-copy">${toTitleCase(motion.movement.label)} with gyro ${motion.movement.gyroDps.toFixed(2)} dps and accel ${motion.movement.accelG.toFixed(3)} g.</p>
    </article>
  `;
}

function renderCalibration(snapshot) {
  const calibration = snapshot.calibration;
  elements.calibrationScore.textContent = `${calibration.confidenceScore.toFixed(0)}%`;
  elements.calibrationNote.textContent = `${toTitleCase(calibration.confidenceLabel)} calibration confidence based on fit, motion, battery, and ${snapshot.eeg.metrics.continuity.label} signal continuity.`;
  elements.calibrationGuide.innerHTML = calibration.preparationGuide
    .map((item) => `<li>${item}</li>`)
    .join("");
}

function renderBands(snapshot) {
  const analysis = snapshot.eeg.metrics.bands.length
    ? snapshot.eeg.metrics.bands
    : computeBandMix(snapshot.eeg.samples, snapshot.eeg.sampleRateHz, snapshot.eeg.channels);
  elements.bandsGrid.innerHTML = analysis
    .map((channel) => {
      const rows = BAND_ORDER.map((band) => {
        const value = channel.mix[band.key] || 0;
        return `
          <div class="band-row">
            <span>${band.label}</span>
            <div class="band-bar"><div class="band-bar-fill" style="width:${value.toFixed(1)}%"></div></div>
            <span class="band-value">${value.toFixed(0)}%</span>
          </div>
        `;
      }).join("");
      return `
        <article class="band-card">
          <h3>${channel.channel}</h3>
          ${rows}
        </article>
      `;
    })
    .join("");
}

function renderMoments(snapshot) {
  const moments = snapshot.eeg.metrics.moments || [];
  const continuity = snapshot.eeg.metrics.continuity;
  const continuityCard = `
    <article class="moment-card">
      <h3>Signal continuity</h3>
      <p class="moment-copy">How even the incoming EEG timing looks across the latest rolling window.</p>
      <div class="moment-stat-grid">
        <div class="moment-stat">Continuity<strong>${continuity.score} %</strong></div>
        <div class="moment-stat">Label<strong>${toTitleCase(continuity.label)}</strong></div>
        <div class="moment-stat">Use<strong>${continuity.score >= 72 ? "Ready" : "Stabilize"}</strong></div>
      </div>
    </article>
  `;
  elements.momentGrid.innerHTML = continuityCard + moments
    .map((moment) => {
      return `
        <article class="moment-card">
          <h3>${moment.channel}</h3>
          <p class="moment-copy">Quick intensity check for this electrode window.</p>
          <div class="moment-stat-grid">
            <div class="moment-stat">Mean abs<strong>${moment.meanAbsUv} uV</strong></div>
            <div class="moment-stat">RMS<strong>${moment.rmsUv} uV</strong></div>
            <div class="moment-stat">Peak<strong>${moment.peakUv} uV</strong></div>
          </div>
        </article>
      `;
    })
    .join("");
}

function pushBandTrendPoint(snapshot) {
  const bands = computeAverageBandMix(snapshot.eeg.metrics.bands);
  state.bandTrendHistory.push({
    at: snapshot.generatedAt,
    bands,
  });
  if (state.bandTrendHistory.length > 90) {
    state.bandTrendHistory.shift();
  }
}

function computeAverageBandMix(channels) {
  const empty = Object.fromEntries(BAND_ORDER.map((band) => [band.key, 0]));
  if (!channels || !channels.length) {
    return empty;
  }
  return Object.fromEntries(
    BAND_ORDER.map((band) => {
      const average =
        channels.reduce((sum, channel) => sum + (channel.mix[band.key] || 0), 0) / channels.length;
      return [band.key, average];
    }),
  );
}

function computeBandMix(samples, sampleRate, channels) {
  if (!samples || samples.length < 32) {
    return channels.map((channel) => ({
      channel,
      mix: Object.fromEntries(BAND_ORDER.map((band) => [band.key, 0])),
    }));
  }

  const windowed = samples.slice(-Math.min(samples.length, 192));
  return channels.map((channel, channelIndex) => {
    const series = windowed.map((sample, index, array) => {
      const ratio = index / Math.max(1, array.length - 1);
      const hann = 0.5 - 0.5 * Math.cos(2 * Math.PI * ratio);
      return sample.values[channelIndex] * hann;
    });

    const totals = {};
    let grandTotal = 0;
    for (const band of BAND_ORDER) {
      let sum = 0;
      for (let frequency = band.range[0]; frequency < band.range[1]; frequency += 1) {
        sum += dftPower(series, sampleRate, frequency);
      }
      totals[band.key] = sum;
      grandTotal += sum;
    }

    const mix = {};
    for (const band of BAND_ORDER) {
      mix[band.key] = grandTotal > 0 ? (totals[band.key] / grandTotal) * 100 : 0;
    }

    return { channel, mix };
  });
}

function dftPower(series, sampleRate, frequency) {
  const length = series.length;
  let real = 0;
  let imaginary = 0;
  for (let index = 0; index < length; index += 1) {
    const angle = (2 * Math.PI * frequency * index) / sampleRate;
    real += series[index] * Math.cos(angle);
    imaginary -= series[index] * Math.sin(angle);
  }
  return real * real + imaginary * imaginary;
}

function drawAllCharts() {
  drawWaveform();
  drawBatteryFitTrend();
  drawBandTrend();
}

function drawWaveform() {
  const snapshot = state.snapshot;
  const canvas = elements.waveformCanvas;
  if (!canvas || !snapshot) {
    return;
  }

  const { ctx, width, height } = prepareCanvas(canvas);
  paintChartBackground(ctx, width, height);
  drawHorizontalGuides(ctx, width, height, 4);

  const samples = snapshot.eeg.samples.slice(-Math.min(snapshot.eeg.samples.length, 240));
  if (!samples.length) {
    return;
  }

  const blockHeight = height / snapshot.eeg.channels.length;
  snapshot.eeg.channels.forEach((channel, channelIndex) => {
    const centerY = blockHeight * channelIndex + blockHeight / 2;
    const values = samples.map((sample) => sample.values[channelIndex]);
    const maxAbs = Math.max(30, ...values.map((value) => Math.abs(value)));

    ctx.strokeStyle = CHANNEL_COLORS[channelIndex % CHANNEL_COLORS.length];
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((value, index) => {
      const x = (index / Math.max(1, values.length - 1)) * width;
      const y = centerY - (value / maxAbs) * (blockHeight * 0.32);
      if (index === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();

    ctx.fillStyle = "rgba(248, 251, 250, 0.84)";
    ctx.font = '600 13px "Avenir Next", "Trebuchet MS", sans-serif';
    ctx.fillText(channel, 14, centerY - blockHeight * 0.34);
  });
}

function drawBatteryFitTrend() {
  const snapshot = state.snapshot;
  const canvas = elements.trendCanvas;
  if (!canvas || !snapshot) {
    return;
  }
  const { ctx, width, height } = prepareCanvas(canvas);
  paintChartBackground(ctx, width, height);
  drawHorizontalGuides(ctx, width, height, 5);

  const battery = (snapshot.battery.history || []).slice(-90).map((item) => item.percent);
  const fit = (snapshot.sensorFit.history || []).slice(-90).map((item) => item.score);
  drawSeries(ctx, width, height, battery, { min: 0, max: 100, color: "#f3b547" });
  drawSeries(ctx, width, height, fit, { min: 0, max: 100, color: "#f06e59" });

  drawLegend(ctx, [
    { label: "Battery", color: "#f3b547" },
    { label: "Fit", color: "#f06e59" },
  ]);
}

function drawBandTrend() {
  const canvas = elements.bandTrendCanvas;
  if (!canvas) {
    return;
  }
  const { ctx, width, height } = prepareCanvas(canvas);
  paintChartBackground(ctx, width, height);
  drawHorizontalGuides(ctx, width, height, 5);

  BAND_ORDER.forEach((band) => {
    const values = state.bandTrendHistory.map((point) => point.bands[band.key] || 0);
    drawSeries(ctx, width, height, values, { min: 0, max: 100, color: band.color });
  });

  drawLegend(
    ctx,
    BAND_ORDER.map((band) => ({
      label: band.label,
      color: band.color,
    })),
  );
}

function prepareCanvas(canvas) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function paintChartBackground(ctx, width, height) {
  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, "rgba(18, 30, 39, 0.96)");
  gradient.addColorStop(1, "rgba(10, 20, 26, 0.96)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, width, height);
}

function drawHorizontalGuides(ctx, width, height, rows) {
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  for (let row = 1; row < rows; row += 1) {
    const y = (height / rows) * row;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
}

function drawSeries(ctx, width, height, values, options) {
  if (!values.length) {
    return;
  }
  const min = options.min;
  const max = options.max;
  ctx.strokeStyle = options.color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = (index / Math.max(1, values.length - 1)) * width;
    const y = height - ((value - min) / Math.max(1, max - min)) * (height - 18) - 9;
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
}

function drawLegend(ctx, entries) {
  ctx.font = '600 12px "Avenir Next", "Trebuchet MS", sans-serif';
  let x = 16;
  entries.forEach((entry) => {
    ctx.fillStyle = entry.color;
    ctx.fillRect(x, 12, 12, 12);
    ctx.fillStyle = "rgba(248, 251, 250, 0.84)";
    ctx.fillText(entry.label, x + 18, 22);
    x += ctx.measureText(entry.label).width + 42;
  });
}

function formatValue(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return value.toFixed(1);
}

function toTitleCase(value) {
  return value.replace(/(^|\s|-)([a-z])/g, (_, start, letter) => `${start}${letter.toUpperCase()}`);
}

start();
