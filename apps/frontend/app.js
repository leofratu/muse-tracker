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
  trustChip: document.getElementById("trust-chip"),
  sourceChip: document.getElementById("source-chip"),
  modelChip: document.getElementById("model-chip"),
  versionChip: document.getElementById("version-chip"),
  sampleRate: document.getElementById("sample-rate"),
  artifactScore: document.getElementById("artifact-score"),
  agreementScore: document.getElementById("agreement-score"),
  usableChannels: document.getElementById("usable-channels"),
  batteryValue: document.getElementById("battery-value"),
  batteryBarFill: document.getElementById("battery-bar-fill"),
  batteryStatus: document.getElementById("battery-status"),
  batteryTrend: document.getElementById("battery-trend"),
  batterySource: document.getElementById("battery-source"),
  batteryNote: document.getElementById("battery-note"),
  supportGrid: document.getElementById("support-grid"),
  sourcesNote: document.getElementById("sources-note"),
  streamGrid: document.getElementById("stream-grid"),
  telemetryNote: document.getElementById("telemetry-note"),
  telemetryGrid: document.getElementById("telemetry-grid"),
  fitScore: document.getElementById("fit-score"),
  fitNote: document.getElementById("fit-note"),
  fitChannelGrid: document.getElementById("fit-channel-grid"),
  plausibilityScore: document.getElementById("plausibility-score"),
  overallBrainNote: document.getElementById("overall-brain-note"),
  overallBandGrid: document.getElementById("overall-band-grid"),
  qualityNote: document.getElementById("quality-note"),
  qualityGrid: document.getElementById("quality-grid"),
  officialFitNote: document.getElementById("official-fit-note"),
  officialFitShell: document.getElementById("official-fit-shell"),
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
  renderQualitySurface(snapshot);
  renderBattery(snapshot);
  renderSources(snapshot);
  renderTelemetry(snapshot);
  renderSupport(snapshot);
  renderFit(snapshot);
  renderBrainState(snapshot);
  renderOfficialFit(snapshot);
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
  const quality = snapshot.eeg.metrics.quality || {};
  const trustLabel = connected
    ? `${toTitleCase(quality.label || "waiting")} trust`
    : "Waiting for signal";
  elements.connectionCopy.textContent = snapshot.connection.statusLine;
  elements.statusChip.textContent = connected ? "Live MuseLSL" : "Waiting for MuseLSL";
  elements.statusChip.style.background = connected
    ? "linear-gradient(135deg, rgba(15,118,110,0.96), rgba(22,158,146,0.96))"
    : "linear-gradient(135deg, rgba(50,79,94,0.96), rgba(96,126,140,0.96))";
  elements.trustChip.textContent = `Trust: ${trustLabel}`;
  elements.trustChip.className = `chip chip-outline ${qualityToneClass(
    quality.artifactScore ?? 0,
  )}`;
  elements.sourceChip.textContent = `Source: ${snapshot.connection.mode}`;
  elements.modelChip.textContent = `Profile: ${snapshot.device.label}`;
  elements.versionChip.textContent = `Version: ${versionName}`;
  elements.sampleRate.textContent = snapshot.eeg.sampleRateHz;
}

function renderQualitySurface(snapshot) {
  const quality = snapshot.eeg.metrics.quality || {};
  const deltaDominance = snapshot.brainState?.deltaDominance || snapshot.eeg.metrics.deltaDominance || {};
  const usable = quality.usableChannelCount ?? 0;
  const blockers = quality.blockers || [];

  elements.artifactScore.textContent = `${Math.round(quality.artifactScore || 0)}%`;
  elements.agreementScore.textContent = `${Math.round(quality.agreementScore || 0)}%`;
  elements.usableChannels.textContent = `${usable}/${snapshot.eeg.channels.length}`;

  elements.qualityNote.textContent = `${quality.summary || "Waiting for enough signal to score artifacts and agreement."} ${blockers[0] || deltaDominance.warning || ""}`.trim();

  const metrics = [
    {
      label: "Accuracy stack",
      value: quality.accuracyScore || 0,
      copy: "Composite trust score from contact, preprocessing, split-window stability, motion, and timing.",
    },
    {
      label: "Artifact control",
      value: quality.artifactScore || 0,
      copy: "Higher means the frontend is seeing fewer spikes, flats, and unstable windows.",
    },
    {
      label: "Channel agreement",
      value: quality.agreementScore || 0,
      copy: "Higher means the sensors tell a more coherent story instead of fighting each other.",
    },
    {
      label: "Drift suppression",
      value: quality.driftScore || 0,
      copy: "Shows how much slow baseline drift has been pushed out before the band estimate.",
    },
    {
      label: "Line-noise rejection",
      value: quality.lineNoiseScore || 0,
      copy: "Higher means less mains contamination from chargers, LEDs, and nearby power supplies.",
    },
    {
      label: "Contact confidence",
      value: quality.contactScore || 0,
      copy: "Summarizes whether the electrodes look stable enough to trust the shared band mix.",
    },
    {
      label: "Split stability",
      value: quality.stabilityScore || 0,
      copy: "Compares the first and second halves of the EEG window so short-lived drift shows up faster.",
    },
    {
      label: "Motion stability",
      value: quality.motionScore || 0,
      copy: "Uses accelerometer and gyroscope stillness to discount windows recorded during movement.",
    },
    {
      label: "Usable sensors",
      value: ((usable / Math.max(snapshot.eeg.channels.length, 1)) * 100) || 0,
      copy: `${usable} of ${snapshot.eeg.channels.length} sensors currently look usable for the combined view.`,
      suffix: `${usable}/${snapshot.eeg.channels.length}`,
    },
  ];

  elements.qualityGrid.innerHTML = metrics
    .map((metric) => {
      const toneClass = qualityToneClass(metric.value);
      return `
        <article class="quality-card ${toneClass}">
          <div class="quality-card-top">
            <span class="support-pill">${metric.label}</span>
            <strong>${metric.suffix || `${Math.round(metric.value)}%`}</strong>
          </div>
          <div class="quality-meter-shell">
            <div class="quality-meter-fill" style="width:${metric.value.toFixed(1)}%"></div>
          </div>
          <p class="fit-copy">${metric.copy}</p>
        </article>
      `;
    })
    .join("");
}

function renderBattery(snapshot) {
  const percent = snapshot.battery.percent;
  const hasBattery = typeof percent === "number";
  elements.batteryValue.textContent = hasBattery ? `${percent.toFixed(1)}%` : "--";
  elements.batteryBarFill.style.width = hasBattery ? `${Math.max(6, percent)}%` : "0%";
  elements.batteryStatus.textContent = toTitleCase(snapshot.battery.level);
  elements.batteryTrend.textContent = toTitleCase(snapshot.battery.trend);
  elements.batterySource.textContent = hasBattery ? snapshot.battery.source : "Awaiting feed";
  elements.batteryNote.textContent = snapshot.connection.telemetryAvailable
    ? "Battery telemetry is flowing through MuseLSL."
    : "Telemetry is not live yet, so battery state stays blank until the headset exposes it.";
}

function renderSources(snapshot) {
  const streams = snapshot.connection.streams || [];
  elements.sourcesNote.textContent = snapshot.connection.connected
    ? "Live source diagnostics show which Muse streams are actively feeding the dashboard."
    : "The dashboard is waiting for MuseLSL streams to appear from your headset.";
  elements.streamGrid.innerHTML = streams
    .map((stream) => {
      const toneClass = stream.status === "live" ? "tone-excellent" : "tone-poor";
      return `
        <article class="support-card ${toneClass}">
          <div class="quality-card-top">
            <span class="support-pill">${toTitleCase(stream.status)}</span>
            <strong>${stream.label}</strong>
          </div>
          <p class="fit-copy">${stream.summary}</p>
          <p class="fit-copy">${stream.detail}</p>
        </article>
      `;
    })
    .join("");
}

function renderTelemetry(snapshot) {
  const telemetry = snapshot.telemetry || {};
  const isActive = Boolean(telemetry.available);
  const history = isActive ? telemetry.history || [] : [];
  elements.telemetryNote.textContent = telemetry.available
    ? "Live Muse telemetry is active. Temperature, fuel gauge, voltage, and battery are now exposed separately."
    : "Waiting for telemetry details.";

  const metrics = [
    {
      label: "Battery",
      value: isActive ? (telemetry.batteryPercent ?? snapshot.battery.percent ?? null) : null,
      suffix: "%",
      series: history.map((point) => point.batteryPercent ?? 0),
      color: "#f3b547",
    },
    {
      label: "Fuel gauge",
      value: isActive ? (telemetry.fuelGaugePercent ?? null) : null,
      suffix: "%",
      series: history.map((point) => point.fuelGaugePercent ?? 0),
      color: "#169e92",
    },
    {
      label: "ADC volt",
      value: isActive ? (telemetry.adcVolt ?? null) : null,
      suffix: " V",
      series: history.map((point) => point.adcVolt ?? 0),
      color: "#f06e59",
    },
    {
      label: "Temp",
      value: isActive ? (telemetry.temperatureC ?? null) : null,
      suffix: " C",
      series: history.map((point) => point.temperatureC ?? 0),
      color: "#c54d74",
    },
  ];

  elements.telemetryGrid.innerHTML = metrics
    .map((metric) => {
      return `
        <article class="quality-card ${qualityToneClass(metric.series.length ? 72 : 24)}">
          <div class="quality-card-top">
            <span class="support-pill">${metric.label}</span>
            <strong>${formatMetricValue(metric.value, metric.suffix)}</strong>
          </div>
          <div class="fit-sparkline-shell">
            ${renderSeriesSparkline(metric.series, {
              min: inferSeriesMin(metric.series),
              max: inferSeriesMax(metric.series),
              color: metric.color,
              emptyLabel: `Waiting for ${metric.label.toLowerCase()}`,
            })}
          </div>
        </article>
      `;
    })
    .join("");
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
  elements.fitNote.textContent = `${toTitleCase(snapshot.sensorFit.overallLabel)} fit. ${snapshot.sensorFit.method} Each sensor card now includes a live contact trend graph.`;
  elements.fitChannelGrid.innerHTML = snapshot.sensorFit.channels
    .map((channel) => {
      const historyGraph = renderFitHistoryGraph(channel.history || []);
      const trendCopy = describeFitTrend(channel.history || []);
      return `
        <article class="fit-card">
          <span class="fit-pill">${toTitleCase(channel.label)}</span>
          <h3>${channel.channel}</h3>
          <div class="fit-meter-shell"><div class="fit-meter-fill" style="width:${channel.score}%"></div></div>
          <div class="fit-sparkline-shell">
            ${historyGraph}
          </div>
          <p class="fit-copy">Trend: ${trendCopy}.</p>
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

function renderBrainState(snapshot) {
  const brainState = snapshot.brainState || {};
  const overallBands = brainState.overallBands || snapshot.eeg.metrics.overallBands || {};
  const dominantBand = brainState.dominantBand || snapshot.eeg.metrics.dominantBand || "waiting";
  const deltaDominance = brainState.deltaDominance || snapshot.eeg.metrics.deltaDominance || {};
  const quality = brainState.quality || snapshot.eeg.metrics.quality || {};
  const plausibility = brainState.plausibilityScore ?? 0;

  elements.plausibilityScore.textContent = `${plausibility.toFixed(0)}%`;
  elements.overallBrainNote.textContent = `${toTitleCase(brainState.plausibilityLabel || "waiting")} plausibility. ${deltaDominance.warning || "Waiting for enough signal to judge the combined band balance."} ${quality.summary || ""}`.trim();
  elements.overallBandGrid.innerHTML = BAND_ORDER.map((band) => {
    const value = overallBands[band.key] || 0;
    const accent = band.key === dominantBand;
    const dominanceCopy = accent ? `<p class="fit-copy">Dominant band across all sensors right now.</p>` : "";
    return `
      <article class="band-card" style="${accent ? "border-color: rgba(16, 118, 110, 0.24); box-shadow: 0 14px 28px rgba(22, 158, 146, 0.10);" : ""}">
        <h3>${band.label}</h3>
        <div class="band-row">
          <span>Mix</span>
          <div class="band-bar"><div class="band-bar-fill" style="width:${value.toFixed(1)}%; background: linear-gradient(90deg, ${band.color}, rgba(22, 158, 146, 0.92));"></div></div>
          <span class="band-value">${value.toFixed(0)}%</span>
        </div>
        <p class="fit-copy">Range ${band.range[0]}-${band.range[1]} Hz.</p>
        ${dominanceCopy}
      </article>
    `;
  }).join("");
}

function renderOfficialFit(snapshot) {
  const officialView = snapshot.sensorFit.officialView || { sensors: [] };
  const score = snapshot.sensorFit.overallScore ?? 0;
  elements.officialFitNote.textContent = `${toTitleCase(snapshot.sensorFit.overallLabel)} fit across the headset. Estimated from live EEG stability to mimic the official calibration view.`;

  const positionMap = {
    TP9: "left: 8px; bottom: 10px;",
    AF7: "left: 52px; top: 4px;",
    AF8: "right: 52px; top: 4px;",
    TP10: "right: 8px; bottom: 10px;",
  };

  const sensorsMarkup = (officialView.sensors || [])
    .map((sensor) => {
      const status = sensor.status || "waiting";
      return `
        <div class="sensor-dot sensor-${status}" style="${positionMap[sensor.channel] || ""}">
          <div>
            <strong>${sensor.channel}</strong>
            <span>${Math.round(sensor.score || 0)}%</span>
            <span>${toTitleCase(sensor.contactState || status)}</span>
          </div>
        </div>
      `;
    })
    .join("");

  elements.officialFitShell.innerHTML = `
    <div class="official-fit-map" aria-label="Live Muse sensor contact view">
      <div class="official-fit-arch"></div>
      ${sensorsMarkup}
      <div class="fit-copy" style="position:absolute; left:50%; bottom:0; transform:translateX(-50%); text-align:center; width:100%;">
        Overall fit ${score.toFixed(0)}%.
      </div>
    </div>
  `;
}

function renderMotion(snapshot) {
  const motion = snapshot.motion;
  const accelerometer = motion.sensors?.accelerometer || {};
  const gyroscope = motion.sensors?.gyroscope || {};
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
    ${renderVectorSensorCard(accelerometer, "#169e92")}
    ${renderVectorSensorCard(gyroscope, "#f06e59")}
  `;
}

function renderCalibration(snapshot) {
  const calibration = snapshot.calibration;
  const quality = snapshot.eeg.metrics.quality || {};
  elements.calibrationScore.textContent = `${calibration.confidenceScore.toFixed(0)}%`;
  elements.calibrationNote.textContent = `${toTitleCase(calibration.confidenceLabel)} calibration confidence based on fit, motion, battery, ${snapshot.eeg.metrics.continuity.label} signal continuity, and ${Math.round(quality.lineNoiseScore || 0)}% line-noise rejection.`;
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
      const qualityWeight = channel.qualityWeight ?? 0;
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
        <article class="band-card ${qualityToneClass(qualityWeight)}">
          <div class="quality-card-top">
            <h3>${channel.channel}</h3>
            <span class="support-pill">Trust ${Math.round(qualityWeight)}%</span>
          </div>
          ${rows}
          <p class="fit-copy">Drift ${percentFromRatio(channel.driftRatio)}%, spikes ${percentFromRatio(channel.spikeRatio)}%, flatline ${percentFromRatio(channel.flatRatio)}%.</p>
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

function renderFitHistoryGraph(history) {
  if (!history.length) {
    return '<div class="fit-sparkline-empty">Waiting for contact history</div>';
  }

  return renderSeriesSparkline(history.slice(-48).map((point) => point.score || 0), {
    min: 0,
    max: 100,
    color: "#0f766e",
    emptyLabel: "Waiting for contact history",
  });
}

function describeFitTrend(history) {
  if (!history.length) {
    return "warming up";
  }

  const recent = history.slice(-12).map((point) => point.score || 0);
  const early = recent.slice(0, Math.max(1, Math.floor(recent.length / 2)));
  const late = recent.slice(Math.floor(recent.length / 2));
  const earlyAvg = early.reduce((sum, value) => sum + value, 0) / Math.max(early.length, 1);
  const lateAvg = late.reduce((sum, value) => sum + value, 0) / Math.max(late.length, 1);
  const delta = lateAvg - earlyAvg;

  if (delta >= 8) {
    return "improving";
  }
  if (delta <= -8) {
    return "slipping";
  }
  return "holding steady";
}

function renderVectorSensorCard(sensor, color) {
  if (!sensor || !sensor.label) {
    return "";
  }
  const history = sensor.history || [];
  const latest = sensor.latest || { x: 0, y: 0, z: 0 };
  const xValues = history.map((point) => point.values?.[0] ?? 0);
  const yValues = history.map((point) => point.values?.[1] ?? 0);
  const zValues = history.map((point) => point.values?.[2] ?? 0);
  const span = inferSymmetricRange([...xValues, ...yValues, ...zValues], sensor.unit === "g" ? 1.2 : 30);
  return `
    <article class="fit-card">
      <span class="fit-pill">${sensor.label}</span>
      <h3>${sensor.available ? `${sensor.sampleRateHz.toFixed(1)} Hz` : "Waiting"}</h3>
      <div class="fit-sparkline-shell">
        ${renderMultiSeriesSparkline([xValues, yValues, zValues], {
          min: -span,
          max: span,
          colors: [color, "#f3b547", "#c54d74"],
          emptyLabel: `Waiting for ${sensor.label.toLowerCase()}`,
        })}
      </div>
      <p class="fit-copy">X ${latest.x?.toFixed?.(3) ?? "--"}, Y ${latest.y?.toFixed?.(3) ?? "--"}, Z ${latest.z?.toFixed?.(3) ?? "--"} ${sensor.unit}.</p>
    </article>
  `;
}

function renderSeriesSparkline(values, options) {
  if (!values.length) {
    return `<div class="fit-sparkline-empty">${options.emptyLabel || "Waiting for history"}</div>`;
  }
  const width = 220;
  const height = 56;
  const polyline = buildPolyline(values, width, height, options.min, options.max);
  return `
    <svg class="fit-sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <rect x="0" y="0" width="${width}" height="${height}" rx="14" ry="14"></rect>
      <path class="fit-sparkline-guide" d="M0 ${height - 4} H${width}"></path>
      <polyline class="fit-sparkline-line" style="stroke:${options.color || "#0f766e"}" points="${polyline}"></polyline>
    </svg>
  `;
}

function renderMultiSeriesSparkline(seriesList, options) {
  const allValues = seriesList.flat().filter((value) => typeof value === "number");
  if (!allValues.length) {
    return `<div class="fit-sparkline-empty">${options.emptyLabel || "Waiting for history"}</div>`;
  }
  const width = 220;
  const height = 56;
  const lines = seriesList
    .map((series, index) => {
      if (!series.length) {
        return "";
      }
      return `<polyline class="fit-sparkline-line" style="stroke:${options.colors[index] || "#0f766e"}" points="${buildPolyline(
        series,
        width,
        height,
        options.min,
        options.max,
      )}"></polyline>`;
    })
    .join("");
  return `
    <svg class="fit-sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <rect x="0" y="0" width="${width}" height="${height}" rx="14" ry="14"></rect>
      <path class="fit-sparkline-guide" d="M0 ${height - 4} H${width}"></path>
      ${lines}
    </svg>
  `;
}

function buildPolyline(values, width, height, min, max) {
  return values
    .map((value, index) => {
      const x = (index / Math.max(values.length - 1, 1)) * width;
      const y = height - (((value - min) / Math.max(max - min, 1)) * (height - 8)) - 4;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function inferSeriesMin(values) {
  if (!values.length) {
    return 0;
  }
  const minimum = Math.min(...values);
  return minimum >= 0 ? 0 : minimum;
}

function inferSeriesMax(values) {
  if (!values.length) {
    return 100;
  }
  const maximum = Math.max(...values);
  return maximum <= 1 ? 1 : maximum * 1.05;
}

function inferSymmetricRange(values, fallback) {
  if (!values.length) {
    return fallback;
  }
  const maxAbs = Math.max(...values.map((value) => Math.abs(value)));
  return Math.max(fallback, maxAbs * 1.12);
}

function formatMetricValue(value, suffix) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return `--${suffix}`;
  }
  return `${value.toFixed(1)}${suffix}`;
}

function percentFromRatio(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return Math.round(value * 100);
}

function qualityToneClass(score) {
  if (score >= 80) {
    return "tone-excellent";
  }
  if (score >= 60) {
    return "tone-good";
  }
  if (score >= 40) {
    return "tone-fair";
  }
  return "tone-poor";
}

function toTitleCase(value) {
  return value.replace(/(^|\s|-)([a-z])/g, (_, start, letter) => `${start}${letter.toUpperCase()}`);
}

start();
