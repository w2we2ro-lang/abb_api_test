"""
ABB Vessel Routing API GUI Tester

Features
- Get OAuth2 token
- Test REST endpoints: ports, weather-limits, conditional-areas, route-network-versions
- Validate a route request using REST /validate-route-request
- Send route calculation request using WebSocket Async API

Install:
    pip install requests websocket-client

Run:
    python abb_vessel_routing_gui.py

Notes
- Token URL / base URLs are based on the provided ABB OpenAPI document.
- You need ABB-issued client_id/client_secret and scopes.
- For a first test, use request type: ShortestPath.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import Any, Dict, Optional

import requests
import websocket


REST_BASE_URL = "https://dev.api.voyageoptimization.abb.com/vessel-routing/v2"
TOKEN_URL = "https://internal.identity.genix.abilityplatform.abb/public/api/oauth2/token"
WS_BASE_URL = "wss://dev.api.voyageoptimization.abb.com/vessel-routing/v2"

ASYNC_ENDPOINTS = {
    "Shortest path": ("/shortest-path", "routing:shortest-path:access"),
    "Instructed set speed": ("/instructed-speed", "routing:instructed-speed:access"),
    "Recommended set speed": ("/recommended-speed", "routing:recommended-speed:access"),
    "Fixed ETA": ("/fixed-eta", "routing:fixed-eta:access"),
    "Optimal set speed": ("/optimal-speed", "routing:optimal-speed:access"),
}

REST_ENDPOINTS = {
    "Ports": "/ports",
    "Weather limits": "/weather-limits",
    "Conditional areas": "/conditional-areas",
    "Route network versions": "/route-network-versions",
}


DEFAULT_SHORTEST_PATH_REQUEST = {
    "type": "ShortestPath",
    "id": "sp-1",
    "points": [
        {
            "type": "Feature",
            "properties": {"name": "Houston, TX", "port": "USHOU-2380"},
            "geometry": {"type": "Point", "coordinates": [-95.2641144, 29.7262421]},
        },
        {
            "type": "Feature",
            "properties": {"name": "Rotterdam (NLRTM)", "port": "NLRTM-2745", "forceRhumbLine": False},
            "geometry": {"type": "Point", "coordinates": [4.0710449, 51.9672394]},
        },
    ],
}


class AbbRoutingGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ABB Vessel Routing API GUI Tester")
        self.geometry("1180x820")

        self.access_token: Optional[str] = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self.after(100, self._flush_log_queue)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        auth = ttk.LabelFrame(root, text="Authentication")
        auth.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.client_id_var = tk.StringVar()
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)

        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        self.client_secret_var = tk.StringVar()
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.scope_var = tk.StringVar(value="routing:rest-api:access routing:shortest-path:access")
        ttk.Entry(auth, textvariable=self.scope_var, width=90).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)

        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(root, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.rest_base_var = tk.StringVar(value=REST_BASE_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        self.ws_base_var = tk.StringVar(value=WS_BASE_URL)
        ttk.Label(urls, text="REST Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.rest_base_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="WS Base").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.ws_base_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=1)
        main.add(right, weight=1)

        rest_frame = ttk.LabelFrame(left, text="REST API Test")
        rest_frame.pack(fill=tk.X, pady=(0, 8))
        self.rest_endpoint_var = tk.StringVar(value="Ports")
        ttk.Combobox(rest_frame, textvariable=self.rest_endpoint_var, values=list(REST_ENDPOINTS.keys()), state="readonly", width=28).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(rest_frame, text="GET", command=self.call_rest_get).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(rest_frame, text="Validate Request", command=self.validate_route_request).grid(row=0, column=2, padx=5, pady=5)

        async_frame = ttk.LabelFrame(left, text="Asynchronous API Test")
        async_frame.pack(fill=tk.X, pady=(0, 8))
        self.async_endpoint_var = tk.StringVar(value="Shortest path")
        ttk.Combobox(async_frame, textvariable=self.async_endpoint_var, values=list(ASYNC_ENDPOINTS.keys()), state="readonly", width=28).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(async_frame, text="Send WebSocket Request", command=self.send_ws_request).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(async_frame, text="Client ID for WS response routing").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.ws_client_id_var = tk.StringVar(value="test-client-001")
        ttk.Entry(async_frame, textvariable=self.ws_client_id_var, width=34).grid(row=1, column=1, sticky="we", padx=5, pady=5)
        async_frame.columnconfigure(1, weight=1)

        req_frame = ttk.LabelFrame(left, text="Route Request JSON")
        req_frame.pack(fill=tk.BOTH, expand=True)
        self.request_text = scrolledtext.ScrolledText(req_frame, height=24, wrap=tk.NONE)
        self.request_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.request_text.insert("1.0", json.dumps(DEFAULT_SHORTEST_PATH_REQUEST, indent=2))

        log_frame = ttk.LabelFrame(right, text="Response / Log")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(root)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Load Sample ShortestPath", command=self.load_sample_shortest_path).pack(side=tk.LEFT, padx=5)

    def load_sample_shortest_path(self) -> None:
        self.request_text.delete("1.0", tk.END)
        self.request_text.insert("1.0", json.dumps(DEFAULT_SHORTEST_PATH_REQUEST, indent=2))

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}\n")

    def _flush_log_queue(self) -> None:
        while not self.log_queue.empty():
            self.log_text.insert(tk.END, self.log_queue.get())
            self.log_text.see(tk.END)
        self.after(100, self._flush_log_queue)

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Access token is empty. Click 'Get Token' first.")
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def _get_request_json(self) -> Dict[str, Any]:
        raw = self.request_text.get("1.0", tk.END).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc

    def get_token(self) -> None:
        def worker() -> None:
            try:
                data = {
                    "client_id": self.client_id_var.get().strip(),
                    "client_secret": self.client_secret_var.get().strip(),
                    "scope": self.scope_var.get().strip(),
                    "grant_type": "client_credentials",
                }
                if not data["client_id"] or not data["client_secret"]:
                    raise ValueError("Client ID and Client Secret are required.")
                self.log("Requesting token...")
                resp = requests.post(self.token_url_var.get().strip(), data=data, timeout=30)
                self.log(f"Token response HTTP {resp.status_code}")
                resp.raise_for_status()
                body = resp.json()
                self.access_token = body["access_token"]
                self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
                self.log("Token saved in memory.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def call_rest_get(self) -> None:
        def worker() -> None:
            try:
                key = self.rest_endpoint_var.get()
                path = REST_ENDPOINTS[key]
                url = self.rest_base_var.get().rstrip("/") + path
                self.log(f"GET {url}")
                resp = requests.get(url, headers=self._headers(), timeout=60)
                self.log(f"HTTP {resp.status_code}")
                self._log_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("REST Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def validate_route_request(self) -> None:
        def worker() -> None:
            try:
                url = self.rest_base_var.get().rstrip("/") + "/validate-route-request"
                payload = self._get_request_json()
                self.log(f"POST {url}")
                resp = requests.post(url, headers={**self._headers(), "Content-Type": "application/json"}, json=payload, timeout=60)
                self.log(f"HTTP {resp.status_code}")
                self._log_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Validation Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def send_ws_request(self) -> None:
        def worker() -> None:
            try:
                if not self.access_token:
                    raise RuntimeError("Access token is empty. Click 'Get Token' first.")
                endpoint_name = self.async_endpoint_var.get()
                path, _scope = ASYNC_ENDPOINTS[endpoint_name]
                url = self.ws_base_var.get().rstrip("/") + path
                payload = self._get_request_json()
                self.log(f"Opening WebSocket: {url}")

                headers = [
                    f"Authorization: Bearer {self.access_token}",
                    f"client_id: {self.ws_client_id_var.get().strip()}",
                ]

                ws = websocket.create_connection(url, header=headers, timeout=30)
                self.log("WebSocket connected. Sending request...")
                ws.send(json.dumps(payload))
                self.log("Request sent. Waiting for response...")

                while True:
                    msg = ws.recv()
                    self.log("WebSocket message received:")
                    try:
                        parsed = json.loads(msg)
                        self.log(json.dumps(parsed, indent=2)[:20000])
                        status = str(parsed.get("status", "")).lower()
                        if status in {"finished", "failed", "success", "completed"}:
                            break
                        # Some responses may not use status. Stop if a linked full response URL exists.
                        if "url" in parsed or "downloadUrl" in parsed or "responseUrl" in parsed:
                            break
                    except json.JSONDecodeError:
                        self.log(msg[:20000])
                        break
                ws.close()
                self.log("WebSocket closed.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("WebSocket Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _log_response(self, resp: requests.Response) -> None:
        try:
            body = resp.json()
            text = json.dumps(body, indent=2)
        except Exception:
            text = resp.text
        self.log(text[:50000])


if __name__ == "__main__":
    app = AbbRoutingGui()
    app.mainloop()
