import {connectDashboard} from "./transport.js";

const $ = id => document.getElementById(id);
const CHANNELS = ["TP9", "AF7", "AF8", "TP10"];
const BANDS = ["delta", "theta", "alpha", "beta", "gamma"];
const COLORS = ["#356c9f", "#438875", "#a46f43", "#876a9a"];
const state = {snapshot: null, mode: "connecting", frozen: false, chartSnapshot: null,
               pending: false, token: null, lastError: null, stop: null};
const format = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : "--";
const text = (id, value) => { $(id).textContent = value; };

function node(tag, value, className) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  if (className) el.className = className;
  return el;
}

function notify(message, error = false) {
  const el = $("notification");
  el.hidden = false;
  el.textContent = message;
  el.classList.toggle("error", error);
}

function visibleSnapshot() {
  const data = state.snapshot;
  if (!data || state.mode !== "offline") return data;
  return {...data, connection: {...data.connection, connected: false, status: "offline"},
          analysis: {available: false, status: "offline", reason: "Backend unavailable. Derived values are hidden.", channels: [], sourceSensors: []},
          telemetry: {}, motion: {}, reference: {available: false, reason: "Backend unavailable."}};
}

function updateControls(data) {
  const online = state.mode !== "offline" && Boolean(data);
  const active = data?.recording?.active;
  const busy = state.pending || data?.recording?.finishing;
  $("record-start").disabled = !online || !data?.connection.connected || active || busy;
  $("record-stop").disabled = !online || !active || busy;
  $("capture-reference").disabled = !online || !data?.analysis.available || busy;
  $("add-marker").disabled = !online || !active || busy;
}

function render() {
  const data = visibleSnapshot();
  updateControls(data);
  if (!data) return;
  const {analysis, connection, recording} = data;
  const status = connection.status;
  const statusCopy = {live: "Live EEG", waiting: "Waiting for headset", stale: "EEG stream stale", offline: "Backend offline"};
  text("connection-status", `${statusCopy[status] || status}${state.mode === "polling" ? " · polling" : ""}`);
  $("connection-status").dataset.state = status;
  text("device-name", `${data.device.label} · ${data.device.name}`);
  text("sample-rate", connection.connected ? `${format(data.device.sampleRateHz, 0)} Hz` : "--");
  text("sensor-count", analysis.channels.length ? `${analysis.coverage} / 4` : "-- / 4");
  text("source-mode", analysis.sourceMode === "frontal-only" ? "Frontal pair only" : analysis.available ? "Combined admitted channels" : "Aggregate withheld");
  text("reliability", Number.isFinite(analysis.reliabilityIndex) ? `${format(analysis.reliabilityIndex, 0)} / 100` : "--");
  text("battery", Number.isFinite(data.telemetry.batteryPercent) ? `${format(data.telemetry.batteryPercent, 0)}%` : "--");
  text("battery-note", data.telemetry.available ? "Live headset-level telemetry" : "Telemetry unavailable or stale");
  text("signal-note", status === "offline" ? "Backend unavailable. Live state cannot be confirmed." : state.frozen ? "Charts frozen. Acquisition and recording continue." : analysis.reason);
  const age = connection.streams.eeg.ageSeconds;
  text("sample-age", status === "offline" ? "Backend offline" : age === null ? "No samples received" : `Latest receipt ${format(age)} s ago`);
  text("record-state", status === "offline" ? "Recording status unknown" : recording.error ? "Recording error" : recording.finishing ? "Finishing recording" : recording.active ? "Recording raw streams" : "Not recording");
  text("record-detail", status === "offline" ? "Reconnect to confirm whether recording is still active." : recording.error || (recording.active ? `${recording.samplesWritten} EEG samples written · ${recording.pendingChunks} queued chunks` : "Raw EEG, motion and event markers"));
  $("record-light").classList.toggle("active", recording.active && status !== "offline");
  const duration = Math.floor(recording.durationSeconds || 0);
  text("record-time", status === "offline" ? "--:--" : `${String(Math.floor(duration / 60)).padStart(2,"0")}:${String(duration % 60).padStart(2,"0")}`);
  if (recording.error && recording.error !== state.lastError) { notify(recording.error, true); state.lastError = recording.error; }
  renderChecks(data);
  renderBands(data);
  renderReference(data);
  renderDetails(data);
  if (!state.frozen) state.chartSnapshot = data;
  drawCharts();
}

function renderChecks(data) {
  const cards = CHANNELS.map((name, index) => {
    const row = data.analysis.channels.find(r => r.channel === name);
    const card = node("div", undefined, "channel-check");
    const top = node("div", undefined, "check-top");
    const title = node("strong");
    title.append(node("span", "", `channel-dot channel-${index}`), node("span", name));
    top.append(title, node("span", row ? row.admitted ? "Included" : "Excluded" : "Waiting", `check-status ${row ? row.admitted ? "included" : "excluded" : ""}`));
    card.append(top, node("p", row ? `RMS ${format(row.rmsUv)} µV · peak–peak ${format(row.peakToPeakUv)} µV` : "No live window available", "check-detail"));
    if (row) card.append(node("p", `Clipped ${format(row.clippingPercent)}% · mains ${format(row.lineNoisePercent)}%`, "check-detail"));
    if (row?.reasons.length) card.append(node("p", row.reasons.join(" · "), "check-reason"));
    return card;
  });
  $("channel-checks").replaceChildren(...cards);
  const timing = data.analysis.timing;
  text("timing-note", timing && Number.isFinite(timing.gaps) ? `${timing.gaps} gaps · ${format(timing.medianJitterPercent, 2)}% median timing deviation · ${data.connection.invalidSamples} invalid samples rejected` : data.connection.lastError || "Waiting for timing diagnostics.");
}

function renderBands(data) {
  const key = $("power-mode").value;
  text("bands-note", key === "relative" ? "Relative power (%) normalized over 1–45 Hz." : "Integrated filtered PSD in µV². Values are not cognitive scores.");
  const rows = CHANNELS.map(name => {
    const channel = data.analysis.channels.find(r => r.channel === name);
    const tr = node("tr", undefined, channel && !channel.admitted ? "excluded" : "");
    const header = node("th", name); header.scope = "row";
    if (channel && !channel.admitted) header.append(node("small", "excluded"));
    tr.append(header, ...BANDS.map(band => node("td", format(channel?.[key]?.[band]))));
    return tr;
  });
  const aggregate = node("tr", undefined, "aggregate");
  const header = node("th", "Mean"); header.scope = "row";
  aggregate.append(header, ...BANDS.map(band => node("td", format(data.analysis.available ? data.analysis[key][band] : null))));
  rows.push(aggregate);
  $("band-rows").replaceChildren(...rows);
}

function renderReference(data) {
  const ref = data.reference;
  text("reference-note", `${ref.label ? `${ref.label}. ` : ""}${ref.reason}`);
  $("reference-shifts").replaceChildren(...BANDS.map(band => {
    const value = ref.available ? ref.relativeShift[band] : null;
    const box = node("div");
    box.append(node("span", band), node("strong", Number.isFinite(value) ? `${value >= 0 ? "+" : ""}${format(value)} pp` : "--"));
    return box;
  }));
}

function definition(label, value) {
  const row = node("div"); row.append(node("dt", label), node("dd", value)); return row;
}

function renderDetails(data) {
  $("stream-list").replaceChildren(...Object.entries(data.connection.streams).map(([kind, stream]) => definition(kind.toUpperCase(), state.mode === "offline" ? "Offline" : stream.live ? "Live" : stream.ageSeconds === null ? "Not received" : "Stale")));
  text("source-identity", `Source ID: ${data.device.id || "none"}. ${data.device.profile === "auto" ? "Model not identified; select --profile explicitly." : "An explicit profile is preserved on connection."}`);
  const tele = data.telemetry;
  const vector = values => Array.isArray(values) ? values.map(v => format(v, 2)).join(", ") : "--";
  $("telemetry-list").replaceChildren(definition("Fuel gauge (raw)", format(tele.fuelGaugeRaw)), definition("ADC (raw)", format(tele.adcRaw)), definition("Temperature (raw)", format(tele.temperatureRaw)), definition("Accelerometer (g)", vector(data.motion.accelerometer)), definition("Gyroscope (°/s)", vector(data.motion.gyroscope)));
  text("processing-version", data.settings.version.toUpperCase());
  text("method-note", `4-second analysis window. 0.5 Hz high-pass, ${data.settings.notchHz ? `${data.settings.notchHz} Hz notch where supported` : "no notch"}, linear segment detrending. Absolute and relative power use the same PSD. No nonlinear clipping is applied to recordings.`);
}

function prepareCanvas(id) {
  const canvas = $(id), width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return null;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) { canvas.width = pixelWidth; canvas.height = pixelHeight; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,width,height);
  ctx.font = "9px ui-monospace, monospace"; ctx.lineWidth = 1;
  return {ctx,width,height};
}

function emptyChart(chart, message) {
  if (!chart) return;
  const {ctx,width,height} = chart;
  ctx.fillStyle = "#738196"; ctx.textAlign = "center";
  ctx.fillText(message, width/2, height/2); ctx.textAlign = "left";
}

function drawWaveforms(data) {
  const chart = prepareCanvas("waveform");
  if (!chart) return;
  const samples = data?.eeg.samples || [];
  if (samples.length < 2) { emptyChart(chart,"Waiting for live EEG samples"); return; }
  const {ctx,width,height} = chart, left = 43, right = width - 8, bottom = height - 24;
  const lane = (bottom - 10)/4, end = samples.at(-1).timestamp, start = end - 4;
  const scale = Number($("wave-scale").value), dt = 1/data.device.sampleRateHz;
  ctx.strokeStyle = "#e8edf3"; ctx.fillStyle = "#8994a3";
  for (let i=0;i<=4;i++) { const x=left+(right-left)*i/4; ctx.beginPath(); ctx.moveTo(x,5); ctx.lineTo(x,bottom); ctx.stroke(); ctx.fillText(`${i-4}s`,x-7,height-7); }
  CHANNELS.forEach((name,index) => {
    const center = 10+lane*(index+.5), mean = samples.reduce((sum,s)=>sum+s.values[index],0)/samples.length;
    ctx.fillStyle = COLORS[index]; ctx.fillText(name,1,center+3);
    ctx.strokeStyle = "#e8edf3"; ctx.beginPath(); ctx.moveTo(left,center); ctx.lineTo(right,center); ctx.stroke();
    ctx.save(); ctx.beginPath(); ctx.rect(left,10+lane*index,right-left,lane); ctx.clip();
    ctx.strokeStyle = COLORS[index]; ctx.lineWidth=1.2; ctx.beginPath();
    let previous = null;
    for (const sample of samples) {
      if (sample.timestamp<start) continue;
      const x=left+(sample.timestamp-start)/4*(right-left), y=center-(sample.values[index]-mean)/scale*(lane*.43);
      if (previous===null || sample.timestamp-previous>dt*1.5) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      previous=sample.timestamp;
    }
    ctx.stroke(); ctx.restore();
  });
}

function drawSpectrum(data) {
  const chart = prepareCanvas("spectrum"); if (!chart) return;
  const channels = data?.analysis.channels || [];
  if (!channels.length) { emptyChart(chart,"A full live window is needed for a spectrum"); return; }
  const {ctx,width,height}=chart, left=38, right=width-10, top=9, bottom=height-25;
  const x=f=>left+(f-1)/44*(right-left), y=db=>bottom-(db+40)/80*(bottom-top);
  ctx.strokeStyle="#e8edf3"; ctx.fillStyle="#8994a3";
  for(let db=-40;db<=40;db+=20){ctx.beginPath();ctx.moveTo(left,y(db));ctx.lineTo(right,y(db));ctx.stroke();ctx.fillText(String(db),3,y(db)+3);}
  for(const f of [1,10,20,30,40,45]){ctx.beginPath();ctx.moveTo(x(f),top);ctx.lineTo(x(f),bottom);ctx.stroke();ctx.fillText(String(f),x(f)-4,height-8);}
  ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
  for(const row of channels){
    if($("spectrum-channel").value!=="all" && row.channel!==$("spectrum-channel").value) continue;
    ctx.strokeStyle=COLORS[CHANNELS.indexOf(row.channel)];ctx.setLineDash(row.admitted?[]:[4,3]);ctx.lineWidth=1.5;ctx.beginPath();let first=true;
    row.frequencies.forEach((frequency,i)=>{if(frequency<1||frequency>45)return;const py=y(10*Math.log10(Math.max(row.psd[i],1e-8)));if(first){ctx.moveTo(x(frequency),py);first=false;}else ctx.lineTo(x(frequency),py);});ctx.stroke();
  }
  ctx.restore();
}

function drawCharts(){drawWaveforms(state.chartSnapshot);drawSpectrum(state.chartSnapshot);}

async function action(path, label) {
  if(state.pending || !state.token) return;
  state.pending=true;updateControls(visibleSnapshot());
  try {
    const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Muse-Token":state.token},body:JSON.stringify(label===undefined?{}:{label})});
    const result=await response.json();
    if(!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    notify(path.endsWith("start")?"Recording started. Raw streams are being written locally.":path.endsWith("stop")?result.error?result.error:result.finishing?"Recording is still finishing. Check the status before exporting.":"Recording saved locally.":path.endsWith("reference")?"Reference captured. Comparisons are descriptive changes in band power.":"Task marker added.",Boolean(result.error));
    if(path.endsWith("stop")) await loadSessions();
  } catch(error) { notify(error.message,true); }
  finally {state.pending=false;updateControls(visibleSnapshot());}
}

async function loadSessions(){
  try{
    const response=await fetch("/api/sessions",{cache:"no-store"});if(!response.ok)throw new Error("Could not read the local archive.");
    const {sessions}=await response.json();
    if(!sessions.length){$("sessions-list").replaceChildren(node("p","No recordings yet. Connect your headset and start a session.","empty-copy"));return;}
    $("sessions-list").replaceChildren(...sessions.map(session=>{
      const item=node("div",undefined,"session-item"),copy=node("div",undefined,"session-copy");
      copy.append(node("strong",session.label),node("small",`${session.startedAt || "Unknown date"} · ${(session.bytes/1024).toFixed(1)} KB · ${session.active?"Recording":session.complete?"Finalized":"Incomplete / interrupted"}`));
      item.append(copy);
      if(!session.active && /^\d{8}T\d{6}Z-[0-9a-f]{8}$/.test(session.id)){
        const links=node("div",undefined,"export-links");
        for(const format of ["jsonl","csv"]){const link=node("a",format.toUpperCase());link.href=`/api/sessions/${encodeURIComponent(session.id)}/export?format=${format}`;link.setAttribute("aria-label",`Export ${session.label} as ${format}`);links.append(link);}item.append(links);
      }
      return item;
    }));
  }catch(error){notify(error.message,true);}
}

$("record-start").addEventListener("click",()=>action("/api/record/start",$("session-label").value));
$("record-stop").addEventListener("click",()=>action("/api/record/stop"));
$("capture-reference").addEventListener("click",()=>action("/api/reference",$("reference-label").value));
$("add-marker").addEventListener("click",()=>action("/api/marker",$("task-label").value));
$("refresh-sessions").addEventListener("click",loadSessions);
$("power-mode").addEventListener("change",render);
$("wave-scale").addEventListener("change",drawCharts);
$("spectrum-channel").addEventListener("change",drawCharts);
$("pause-chart").addEventListener("click",()=>{state.frozen=!state.frozen;$("pause-chart").setAttribute("aria-pressed",String(state.frozen));text("pause-chart",state.frozen?"Resume charts":"Freeze charts");render();});
window.addEventListener("resize",drawCharts);
function start(){if(state.stop)return;state.stop=connectDashboard(snapshot=>{
  const previous=state.snapshot;
  if(previous && snapshot.epoch===previous.epoch && (snapshot.sequence<previous.sequence || (snapshot.sequence===previous.sequence && snapshot.generatedAt<previous.generatedAt)))return;
  state.snapshot=snapshot;state.token=snapshot.controlToken;render();
},mode=>{state.mode=mode;render();});loadSessions();}
window.addEventListener("pagehide",()=>{state.stop?.();state.stop=null;});
window.addEventListener("pageshow",start);
start();
