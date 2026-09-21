"""Browser smoke checks using an explicitly synthetic test fixture, never production demo data."""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.backend.app import MuseDashboardServer
from apps.backend.muse_lsl_bridge import MuseLSLBridge


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, default=Path("test-results/browser"))
    parser.add_argument("--offline-browser", action="store_true", help="Use a Python HTTP bridge when browser networking is administratively disabled; this does not test native SSE networking.")
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    stopped, running = threading.Event(), threading.Event()
    with tempfile.TemporaryDirectory() as directory:
        bridge = MuseLSLBridge(profile_key="muse-1", record_dir=Path(directory))
        server = MuseDashboardServer(("127.0.0.1", 0), bridge)
        http = threading.Thread(target=server.serve_forever, daemon=True)
        http.start()

        def produce():
            index = 0
            while not stopped.is_set():
                if running.is_set():
                    pairs = []
                    for _ in range(16):
                        t = 100 + index / 256
                        pairs.append((t, [20 * math.sin(2 * math.pi * (10.6 + c * .1) * t)
                                          + 3 * math.sin(2 * math.pi * 19 * t) for c in range(4)]))
                        index += 1
                    bridge.ingest("eeg", pairs)
                    bridge.ingest("acc", [(t, [0.01, 0.01, 1.0])])
                    bridge.ingest("gyro", [(t, [0.1, 0.2, 0.1])])
                    bridge.ingest("telemetry", [(t, [83, 1122, 3400, 30])])
                bridge.refresh()
                stopped.wait(.025)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        errors, status_requests = [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=os.getenv("CHROMIUM_EXECUTABLE"),
                                                       args=["--no-sandbox"])
                page = browser.new_page(viewport={"width":1440, "height":1100}, device_scale_factor=1,
                                        reduced_motion="reduce")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: status_requests.append(request.url) if request.url.endswith("/api/status") else None)
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                if args.offline_browser:
                    frontend = Path(__file__).resolve().parents[1] / "apps" / "frontend"
                    shell = (frontend / "index.html").read_text()
                    shell = shell.replace('<link rel="stylesheet" href="/styles.css">', '')
                    shell = shell.replace('<script type="module" src="/app.js"></script>', '')
                    page.set_content(shell)
                    page.add_style_tag(path=str(frontend / "styles.css"))

                    def proxy(path, options=None):
                        if not path.startswith("/api/") or path.startswith("//"):
                            raise ValueError("Only local test API requests are permitted.")
                        options = options or {}
                        body = options.get("body")
                        request = Request(base_url + path, data=body.encode() if body is not None else None,
                                          headers=options.get("headers") or {}, method=options.get("method") or "GET")
                        try:
                            with urlopen(request, timeout=6) as response:
                                return {"ok": True, "status": response.status, "text": response.read().decode()}
                        except HTTPError as exc:
                            return {"ok": False, "status": exc.code, "text": exc.read().decode()}
                        except URLError:
                            return {"ok": False, "status": 503, "text": '{}'}

                    page.expose_function("__museHttp", proxy)
                    page.add_script_tag(content="""
                      window.fetch = async (path, options={}) => {
                        const r = await window.__museHttp(path, {method:options.method,headers:options.headers,body:options.body});
                        return {ok:r.ok,status:r.status,json:async()=>JSON.parse(r.text)};
                      };
                      window.EventSource = class extends EventTarget {
                        constructor() { super(); this.closed=false; this.tick(); }
                        async tick() {
                          if(this.closed)return;
                          try { const r=await window.__museHttp('/api/status',{});
                            if(!r.ok)throw new Error('offline');
                            if(!this.closed)this.dispatchEvent(new MessageEvent('snapshot',{data:r.text}));
                          } catch { if(!this.closed)this.onerror?.(); }
                          if(!this.closed)this.timer=setTimeout(()=>this.tick(),400);
                        }
                        close(){this.closed=true;clearTimeout(this.timer);}
                      };
                    """)
                    script = (frontend / "transport.js").read_text().replace("export function connectDashboard", "function connectDashboard")
                    script += "\n" + (frontend / "app.js").read_text().replace('import {connectDashboard} from "./transport.js";', '')
                    page.add_script_tag(content=script)
                else:
                    page.goto(base_url)
                expect(page.locator("#connection-status")).to_contain_text("Waiting")
                expect(page.locator("#record-start")).to_be_disabled()
                assert page.locator("#reliability").inner_text() == "--"
                page.screenshot(path=str(args.screenshots / "waiting-desktop.png"), full_page=True)

                bridge.configure_source("Muse 1 · synthetic browser fixture", "MuseTestFixture", 256)
                running.set()
                expect(page.locator("#sensor-count")).to_have_text("4 / 4", timeout=10000)
                expect(page.locator("#record-start")).to_be_enabled()
                request_count = len(status_requests)
                page.wait_for_timeout(1700)
                if not args.offline_browser:
                    assert len(status_requests) == request_count, "Polling continued while SSE was healthy"
                page.screenshot(path=str(args.screenshots / "live-desktop.png"), full_page=True)

                page.locator("#session-label").fill("Synthetic browser test")
                page.locator("#record-start").click()
                expect(page.locator("#record-state")).to_have_text("Recording raw streams")
                page.locator("#add-marker").click()
                expect(page.locator("#notification")).to_contain_text("Task marker added")
                page.locator("#capture-reference").click()
                expect(page.locator("#reference-note")).to_contain_text("Eyes open, seated")
                page.locator("#pause-chart").focus()
                page.keyboard.press("Enter")
                expect(page.locator("#pause-chart")).to_have_attribute("aria-pressed", "true")
                expect(page.locator("#record-state")).to_have_text("Recording raw streams")
                page.keyboard.press("Enter")

                page.set_viewport_size({"width":390, "height":844})
                page.evaluate("window.scrollTo(0,0)")
                page.wait_for_timeout(150)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile horizontal overflow"
                assert page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
                page.screenshot(path=str(args.screenshots / "live-mobile.png"), full_page=True)

                with bridge._lock:
                    bridge._source["name"] = '<img src=x onerror="window.__xss=true">'
                expect(page.locator("#device-name")).to_contain_text("<img")
                assert page.locator("img").count() == 0
                assert page.evaluate("window.__xss === undefined")

                running.clear()
                with bridge._lock:
                    bridge._received["eeg"] -= 10
                expect(page.locator("#connection-status")).to_contain_text("stale")
                expect(page.locator("#capture-reference")).to_be_disabled()
                expect(page.locator("#reliability")).to_have_text("--")
                page.locator("#record-stop").click()
                expect(page.locator("#record-state")).to_have_text("Not recording")
                expect(page.locator("#sessions-list")).to_contain_text("Finalized")
                assert page.locator("#sessions-list a").count() == 2

                # A server outage must not leave successful recording/live indicators behind.
                stopped.set()
                producer.join(2)
                server.shutdown()
                server.server_close()
                expect(page.locator("#connection-status")).to_contain_text("Backend offline", timeout=10000)
                expect(page.locator("#record-state")).to_have_text("Recording status unknown")
                expect(page.locator("#record-start")).to_be_disabled()
                assert not errors, errors
                browser.close()
        finally:
            stopped.set()
            producer.join(2)
            if http.is_alive():
                server.shutdown()
            server.server_close()
            http.join(2)
    print("Browser checks passed: waiting/live/stale/offline, record/mark/reference/export links, XSS, keyboard, mobile, reduced motion.")
    print("Networking: Python HTTP bridge; native browser SSE was not tested." if args.offline_browser else "Networking: native browser HTTP/SSE; no background polling with healthy SSE.")
    print(f"Screenshots: {args.screenshots}")


if __name__ == "__main__":
    main()
