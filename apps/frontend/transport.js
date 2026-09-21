// Native EventSource owns reconnection. Poll only while SSE is unavailable or silent.
export function connectDashboard(onSnapshot, onState, env = window) {
  let disposed = false, source = null, pollTimer = null, inflight = null;
  let lastSeen = Date.now(), mode = "connecting";
  const emit = value => { if (!disposed && value !== mode) { mode = value; onState(value); } };
  const stopPolling = () => { if (pollTimer !== null) env.clearInterval(pollTimer); pollTimer = null; };
  async function poll() {
    if (disposed || inflight) return;
    const controller = new AbortController();
    inflight = controller;
    const timeout = env.setTimeout(() => controller.abort(), 2500);
    try {
      const response = await env.fetch("/api/status", {signal: controller.signal, cache: "no-store"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (payload.schemaVersion !== 2) throw new Error("Unsupported API schema");
      if (!disposed) { lastSeen = Date.now(); onSnapshot(payload); if (mode !== "sse") emit("polling"); }
    } catch { if (mode !== "sse") emit("offline"); }
    finally { env.clearTimeout(timeout); inflight = null; }
  }
  function startPolling() {
    if (pollTimer === null && !disposed) { poll(); pollTimer = env.setInterval(poll, 1500); }
  }
  poll();
  if (env.EventSource) {
    source = new env.EventSource("/api/stream");
    source.addEventListener("snapshot", event => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.schemaVersion !== 2) throw new Error("Unsupported API schema");
        if (!disposed) { lastSeen = Date.now(); emit("sse"); stopPolling(); onSnapshot(payload); }
      } catch { emit("offline"); startPolling(); }
    });
    source.onerror = () => { emit("offline"); startPolling(); };
  } else startPolling();
  const watchdog = env.setInterval(() => {
    if (Date.now() - lastSeen > 3000) { emit("offline"); startPolling(); }
  }, 1000);
  return () => {
    disposed = true; source?.close(); stopPolling();
    env.clearInterval(watchdog); inflight?.abort();
  };
}
