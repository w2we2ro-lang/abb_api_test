"""
ABB API GUI Tester

One Tkinter app for:
- Vessel Routing API tests
- Voyage Configuration API tests

Run:
    python abb_api_gui.py
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, Optional

import requests
import websocket


TOKEN_URL = "https://internal.identity.genix.abilityplatform.abb/public/api/oauth2/token"

VESSEL_REST_BASE_URL = "https://dev.api.voyageoptimization.abb.com/vessel-routing/v2"
VESSEL_WS_BASE_URL = "wss://dev.api.voyageoptimization.abb.com/vessel-routing/v2"
VESSEL_DEFAULT_SCOPE = "routing:rest-api:access routing:shortest-path:access"

VOYAGE_API_BASE_URL = "https://dev.api.voyageoptimization.abb.com"
VOYAGE_DEFAULT_SCOPE = (
    "voyage:route-calculation-schedule:read "
    "voyage:route-calculation-schedule:write "
    "voyage:route-advice:access "
    "voyage:route-comparison:access"
)

VESSEL_ASYNC_ENDPOINTS = {
    "Shortest path": ("/shortest-path", "routing:shortest-path:access"),
    "Instructed set speed": ("/instructed-speed", "routing:instructed-speed:access"),
    "Recommended set speed": ("/recommended-speed", "routing:recommended-speed:access"),
    "Fixed ETA": ("/fixed-eta", "routing:fixed-eta:access"),
    "Optimal set speed": ("/optimal-speed", "routing:optimal-speed:access"),
}

VESSEL_REST_ENDPOINTS = {
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

VOYAGE_ENDPOINTS = {
    "Create Route Calculation Schedule": {
        "method": "POST",
        "path": "/voyage/configuration/v1/route-calculation-schedule",
        "body": "schedule",
    },
    "Get Route Calculation Schedule": {
        "method": "GET",
        "path": "/voyage/configuration/v1/route-calculation-schedule/{routeCalculationScheduleId}",
        "needs_schedule_id": True,
        "query": ["revision"],
    },
    "Update Route Calculation Schedule": {
        "method": "PATCH",
        "path": "/voyage/configuration/v1/route-calculation-schedule/{routeCalculationScheduleId}",
        "needs_schedule_id": True,
        "body": "schedule_patch",
    },
    "Search Route Calculation Schedules": {
        "method": "GET",
        "path": "/voyage/configuration/v1/route-calculation-schedule/search",
        "query": ["imo", "from", "to", "status", "customerReferenceId", "routeCalculationScheduleId"],
    },
    "Create Route Advice Report": {
        "method": "POST",
        "path": "/voyage/configuration/v1/route-advice",
        "body": "route_advice",
    },
    "Create Route Comparison Report": {
        "method": "POST",
        "path": "/voyage/configuration/v1/route-comparison",
        "body": "route_comparison",
    },
}

VOYAGE_SAMPLES: Dict[str, Dict[str, Any]] = {
    "schedule": {
        "customerReferenceId": "123456789",
        "optimizationInfo": {
            "id": "opt-123",
            "name": "Fuel Efficient Route",
            "comments": "Optimized for minimal fuel consumption",
        },
        "voyageInfo": {
            "id": "ABC-123",
            "name": "San Antonio - Recalada, via Cape Horn",
            "comments": "Route calculation schedule test.",
        },
        "scheduler": {
            "schedule": "0 0,6,12,18 * * *",
            "activeDays": 60,
            "deactivationDistanceNm": 10,
        },
        "routeRequest": {
            "routeSourceType": "PreCalculated",
            "routingCorrelationId": "K4w-THf4DoEERsg=",
        },
    },
    "schedule_patch": {
        "scheduler": {
            "schedule": "0 0,12 * * *",
            "activeDays": 30,
            "deactivationDistanceNm": 10,
        },
        "voyageInfo": {
            "id": "ABC-123",
            "name": "San Antonio - Recalada, via Cape Horn",
            "comments": "Updated schedule test.",
        },
    },
    "route_advice": {
        "routingCorrelationId": "K4w-THf4DoEERsg=",
        "customerReferenceId": "123456789",
        "routeSourceType": "PreCalculated",
        "updateType": "RouteAdvice",
        "voyageInfo": {
            "id": "ABC-123",
            "name": "San Antonio - Recalada, via Cape Horn",
            "comments": "Route advice unchanged. No significant changes in the weather forecast.",
        },
    },
    "route_comparison": {
        "routes": [
            {"routingCorrelationId": "K4w-THf4DoEERsg=", "name": "Route 1"},
            {"routingCorrelationId": "K4w-THf4DoEERsf=", "name": "Route 2"},
        ],
        "customerReferenceId": "123456789",
        "includeCosts": True,
        "includeFuelConsumption": True,
        "keyWeatherEvents": ["2026-07-06T00:00:00Z"],
        "voyageInfo": {
            "id": "ABC-123",
            "name": "Same Route",
            "comments": "This is the comparison of route advices for your voyage.",
        },
    },
}


class LogMixin:
    def _init_log_queue(self) -> None:
        self.log_queue: queue.Queue[str] = queue.Queue()

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}\n")

    def _flush_log_queue(self) -> None:
        while not self.log_queue.empty():
            self.log_text.insert(tk.END, self.log_queue.get())
            self.log_text.see(tk.END)
        self.after(100, self._flush_log_queue)

    def _log_response(self, resp: requests.Response) -> None:
        try:
            text = json.dumps(resp.json(), indent=2)
        except Exception:
            text = resp.text
        self.log(text[:50000])

    def _log_rate_limit_headers(self, resp: requests.Response) -> None:
        rate_headers = {
            key: resp.headers.get(key)
            for key in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")
            if resp.headers.get(key) is not None
        }
        if rate_headers:
            self.log(f"Rate limit: {json.dumps(rate_headers)}")


class VesselRoutingFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.access_token: Optional[str] = None
        self._init_log_queue()
        self._build_ui()
        self.after(100, self._flush_log_queue)

    def _build_ui(self) -> None:
        auth = ttk.LabelFrame(self, text="Authentication")
        auth.pack(fill=tk.X, pady=(0, 8))

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.scope_var = tk.StringVar(value=VESSEL_DEFAULT_SCOPE)

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)
        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.scope_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(self, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.rest_base_var = tk.StringVar(value=VESSEL_REST_BASE_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        self.ws_base_var = tk.StringVar(value=VESSEL_WS_BASE_URL)
        ttk.Label(urls, text="REST Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.rest_base_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="WS Base").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.ws_base_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=1)
        main.add(right, weight=1)

        rest_frame = ttk.LabelFrame(left, text="REST API Test")
        rest_frame.pack(fill=tk.X, pady=(0, 8))
        self.rest_endpoint_var = tk.StringVar(value="Ports")
        ttk.Combobox(rest_frame, textvariable=self.rest_endpoint_var, values=list(VESSEL_REST_ENDPOINTS.keys()), state="readonly", width=28).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(rest_frame, text="GET", command=self.call_rest_get).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(rest_frame, text="Validate Request", command=self.validate_route_request).grid(row=0, column=2, padx=5, pady=5)

        async_frame = ttk.LabelFrame(left, text="Asynchronous API Test")
        async_frame.pack(fill=tk.X, pady=(0, 8))
        self.async_endpoint_var = tk.StringVar(value="Shortest path")
        ttk.Combobox(async_frame, textvariable=self.async_endpoint_var, values=list(VESSEL_ASYNC_ENDPOINTS.keys()), state="readonly", width=28).grid(row=0, column=0, padx=5, pady=5)
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

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Load Sample ShortestPath", command=self.load_sample_shortest_path).pack(side=tk.LEFT, padx=5)

    def load_sample_shortest_path(self) -> None:
        self.request_text.delete("1.0", tk.END)
        self.request_text.insert("1.0", json.dumps(DEFAULT_SHORTEST_PATH_REQUEST, indent=2))

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
                path = VESSEL_REST_ENDPOINTS[self.rest_endpoint_var.get()]
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
                path, _scope = VESSEL_ASYNC_ENDPOINTS[self.async_endpoint_var.get()]
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


class VoyageConfigurationFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.access_token: Optional[str] = None
        self._init_log_queue()
        self._build_ui()
        self.after(100, self._flush_log_queue)
        self.load_sample()

    def _build_ui(self) -> None:
        auth = ttk.LabelFrame(self, text="Authentication")
        auth.pack(fill=tk.X, pady=(0, 8))

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.scope_var = tk.StringVar(value=VOYAGE_DEFAULT_SCOPE)

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)
        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.scope_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(self, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.api_base_var = tk.StringVar(value=VOYAGE_API_BASE_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        ttk.Label(urls, text="API Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.api_base_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        controls = ttk.LabelFrame(self, text="Request")
        controls.pack(fill=tk.X, pady=(0, 8))

        self.endpoint_var = tk.StringVar(value="Create Route Calculation Schedule")
        endpoint_box = ttk.Combobox(controls, textvariable=self.endpoint_var, values=list(VOYAGE_ENDPOINTS.keys()), state="readonly", width=38)
        endpoint_box.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        endpoint_box.bind("<<ComboboxSelected>>", lambda _event: self.load_sample())
        ttk.Button(controls, text="Load Sample", command=self.load_sample).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(controls, text="Send Request", command=self.send_request).grid(row=0, column=2, padx=5, pady=5)

        self.schedule_id_var = tk.StringVar()
        self.revision_var = tk.StringVar()
        self.imo_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Active")
        self.customer_ref_var = tk.StringVar()
        self.search_schedule_id_var = tk.StringVar()

        ttk.Label(controls, text="Schedule ID").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.schedule_id_var).grid(row=1, column=1, columnspan=2, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Revision").grid(row=1, column=3, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.revision_var, width=16).grid(row=1, column=4, sticky="we", padx=5, pady=3)

        ttk.Label(controls, text="IMO").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.imo_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="From").grid(row=2, column=2, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.from_var).grid(row=2, column=3, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="To").grid(row=2, column=4, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.to_var).grid(row=2, column=5, sticky="we", padx=5, pady=3)

        ttk.Label(controls, text="Status").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(controls, textvariable=self.status_var, values=["Active", "Inactive", "All"], state="readonly", width=12).grid(row=3, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(controls, text="Customer Ref").grid(row=3, column=2, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.customer_ref_var).grid(row=3, column=3, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Search Schedule ID").grid(row=3, column=4, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.search_schedule_id_var).grid(row=3, column=5, sticky="we", padx=5, pady=3)
        for col in range(6):
            controls.columnconfigure(col, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)
        request_frame = ttk.LabelFrame(main, text="JSON Body")
        response_frame = ttk.LabelFrame(main, text="Response / Log")
        main.add(request_frame, weight=1)
        main.add(response_frame, weight=1)

        self.request_text = scrolledtext.ScrolledText(request_frame, height=28, wrap=tk.NONE)
        self.request_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text = scrolledtext.ScrolledText(response_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)

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
                self._log_rate_limit_headers(resp)
                resp.raise_for_status()
                body = resp.json()
                self.access_token = body["access_token"]
                self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
                self.log("Token saved in memory.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def send_request(self) -> None:
        def worker() -> None:
            try:
                if not self.access_token:
                    raise RuntimeError("Access token is empty. Click 'Get Token' first.")
                endpoint = VOYAGE_ENDPOINTS[self.endpoint_var.get()]
                method = endpoint["method"]
                path = self._build_path(endpoint)
                url = self.api_base_var.get().rstrip("/") + path
                params = self._build_params(endpoint)
                body = self._get_request_json() if endpoint.get("body") else None

                self.log(f"{method} {url}")
                if params:
                    self.log(f"Query: {json.dumps(params, indent=2)}")
                resp = requests.request(method, url, headers=self._headers(body is not None), params=params, json=body, timeout=60)
                self.log(f"HTTP {resp.status_code}")
                self._log_rate_limit_headers(resp)
                self._log_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Request Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def load_sample(self) -> None:
        endpoint = VOYAGE_ENDPOINTS[self.endpoint_var.get()]
        sample_key = endpoint.get("body")
        self.request_text.delete("1.0", tk.END)
        if sample_key:
            self.request_text.insert("1.0", json.dumps(VOYAGE_SAMPLES[sample_key], indent=2))

    def _build_path(self, endpoint: Dict[str, Any]) -> str:
        path = endpoint["path"]
        if endpoint.get("needs_schedule_id"):
            schedule_id = self.schedule_id_var.get().strip()
            if not schedule_id:
                raise ValueError("Schedule ID is required for this endpoint.")
            path = path.replace("{routeCalculationScheduleId}", schedule_id)
        return path

    def _build_params(self, endpoint: Dict[str, Any]) -> Dict[str, str]:
        requested = endpoint.get("query", [])
        fields = {
            "revision": self.revision_var,
            "imo": self.imo_var,
            "from": self.from_var,
            "to": self.to_var,
            "status": self.status_var,
            "customerReferenceId": self.customer_ref_var,
            "routeCalculationScheduleId": self.search_schedule_id_var,
        }
        return {name: fields[name].get().strip() for name in requested if fields[name].get().strip()}

    def _headers(self, has_body: bool) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _get_request_json(self) -> Dict[str, Any]:
        raw = self.request_text.get("1.0", tk.END).strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed


class AbbApiGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ABB API GUI Tester")
        self.geometry("1240x880")

        tabs = ttk.Notebook(self)
        tabs.pack(fill=tk.BOTH, expand=True)
        tabs.add(VesselRoutingFrame(tabs), text="Vessel Routing API")
        tabs.add(VoyageConfigurationFrame(tabs), text="Voyage Configuration API")


def main() -> None:
    app = AbbApiGui()
    app.mainloop()


if __name__ == "__main__":
    main()
