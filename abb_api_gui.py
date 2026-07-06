"""
ABB API GUI Tester

One Tkinter app for:
- Vessel Routing API tests
- Voyage Configuration API tests
- Product API tests
- Notification API tests

Run:
    python abb_api_gui.py
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Tuple

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
PRODUCT_BASE_URL = "https://dev.api.voyageoptimization.abb.com/voyage/products/v1"
PRODUCT_DEFAULT_SCOPE = "product:report:access"
NOTIFICATION_REST_BASE_URL = "https://dev.api.voyageoptimization.abb.com"
NOTIFICATION_WS_URL = "wss://dev.api.voyageoptimization.abb.com/voyage/notification/v1/ws/products"
NOTIFICATION_DEFAULT_SCOPE = "notification:report:read"

ENVIRONMENT_URLS = {
    "dev": {
        "token": TOKEN_URL,
        "vessel_rest": VESSEL_REST_BASE_URL,
        "vessel_ws": VESSEL_WS_BASE_URL,
        "voyage_api": VOYAGE_API_BASE_URL,
        "product_base": PRODUCT_BASE_URL,
        "notification_rest": NOTIFICATION_REST_BASE_URL,
        "notification_ws": NOTIFICATION_WS_URL,
    },
    "prod": {
        "token": TOKEN_URL,
        "vessel_rest": "https://api.voyageoptimization.abb.com/vessel-routing/v2",
        "vessel_ws": "wss://api.voyageoptimization.abb.com/vessel-routing/v2",
        "voyage_api": "https://api.voyageoptimization.abb.com",
        "product_base": "https://api.voyageoptimization.abb.com/voyage/products/v1",
        "notification_rest": "https://api.voyageoptimization.abb.com",
        "notification_ws": "wss://api.voyageoptimization.abb.com/voyage/notification/v1/ws/products",
    },
}

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

PRODUCT_ENDPOINTS = {
    "Get Product Status": {
        "path": "/reports/{correlationId}",
        "needs_correlation_id": True,
    },
    "Download Product": {
        "path": "/reports/{correlationId}/{filename}",
        "needs_correlation_id": True,
        "needs_filename": True,
    },
    "Get Last Sent Product": {
        "path": "/reports/last-sent",
        "query": ["imo", "productName", "customerReferenceId"],
    },
    "Search Products": {
        "path": "/reports/search",
        "query": [
            "imo",
            "from",
            "to",
            "productName",
            "status",
            "customerReferenceId",
            "voyageInfoId",
            "routeCalculationScheduleId",
        ],
    },
}


class LogMixin:
    def _init_log_queue(self) -> None:
        self.log_queue: queue.Queue[str] = queue.Queue()

    def _remember_auth(self, data: Dict[str, str], body: Dict[str, Any]) -> None:
        self.access_token = body["access_token"]
        root = self.winfo_toplevel()
        if hasattr(root, "shared_auth"):
            root.shared_auth.update(
                {
                    "client_id": data.get("client_id", ""),
                    "client_secret": data.get("client_secret", ""),
                    "scope": data.get("scope", ""),
                    "token_url": self.token_url_var.get().strip(),
                    "access_token": self.access_token,
                    "expires_at": time.time() + int(body.get("expires_in", 0) or 0),
                }
            )

    def use_shared_auth(self) -> None:
        root = self.winfo_toplevel()
        shared = getattr(root, "shared_auth", {})
        if not shared.get("access_token") and not shared.get("client_id"):
            messagebox.showinfo("Shared Auth", "No shared auth is available yet.")
            return
        for name, key in (
            ("client_id_var", "client_id"),
            ("client_secret_var", "client_secret"),
            ("scope_var", "scope"),
            ("token_url_var", "token_url"),
        ):
            if hasattr(self, name) and shared.get(key):
                getattr(self, name).set(shared[key])
        self.access_token = shared.get("access_token") or self.access_token
        self.log("Shared auth copied into this tab.")

    def _effective_token(self) -> Optional[str]:
        if self.access_token:
            return self.access_token
        root = self.winfo_toplevel()
        shared = getattr(root, "shared_auth", {})
        return shared.get("access_token")

    def _require_token(self) -> str:
        token = self._effective_token()
        if not token:
            raise RuntimeError("Access token is empty. Click 'Get Token' or 'Use Shared Auth' first.")
        return token

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
            parsed = resp.json()
            self.latest_response_data = parsed
            text = json.dumps(parsed, indent=2)
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

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(self.log_text.get("1.0", tk.END), encoding="utf-8")
            self.log(f"Log saved: {path}")

    def load_json_preset(self) -> None:
        if not hasattr(self, "request_text"):
            return
        path = filedialog.askopenfilename(
            title="Load JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.request_text.delete("1.0", tk.END)
            self.request_text.insert("1.0", Path(path).read_text(encoding="utf-8"))
            self.log(f"JSON loaded: {path}")

    def save_json_preset(self) -> None:
        if not hasattr(self, "request_text"):
            return
        path = filedialog.asksaveasfilename(
            title="Save JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(self.request_text.get("1.0", tk.END).strip() + "\n", encoding="utf-8")
            self.log(f"JSON saved: {path}")


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
        ttk.Button(auth, text="Use Shared Auth", command=self.use_shared_auth).grid(row=0, column=5, rowspan=2, padx=5, pady=5, sticky="ns")

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
        ttk.Button(bottom, text="Save Log", command=self.save_log).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Load JSON", command=self.load_json_preset).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Save JSON", command=self.save_json_preset).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Load Sample ShortestPath", command=self.load_sample_shortest_path).pack(side=tk.LEFT, padx=5)

    def load_sample_shortest_path(self) -> None:
        self.request_text.delete("1.0", tk.END)
        self.request_text.insert("1.0", json.dumps(DEFAULT_SHORTEST_PATH_REQUEST, indent=2))

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._require_token()}", "Accept": "application/json"}

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
                self._remember_auth(data, body)
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
                path, _scope = VESSEL_ASYNC_ENDPOINTS[self.async_endpoint_var.get()]
                url = self.ws_base_var.get().rstrip("/") + path
                payload = self._get_request_json()
                self.log(f"Opening WebSocket: {url}")

                headers = [
                    f"Authorization: Bearer {self._require_token()}",
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
        ttk.Button(auth, text="Use Shared Auth", command=self.use_shared_auth).grid(row=0, column=5, rowspan=2, padx=5, pady=5, sticky="ns")

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
        ttk.Button(bottom, text="Save Log", command=self.save_log).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Load JSON", command=self.load_json_preset).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Save JSON", command=self.save_json_preset).pack(side=tk.LEFT, padx=5)

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
                self._remember_auth(data, body)
                self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
                self.log("Token saved in memory.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def send_request(self) -> None:
        def worker() -> None:
            try:
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
        headers = {"Authorization": f"Bearer {self._require_token()}", "Accept": "application/json"}
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


class ProductApiFrame(ttk.Frame, LogMixin):
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
        self.scope_var = tk.StringVar(value=PRODUCT_DEFAULT_SCOPE)

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)
        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")
        ttk.Button(auth, text="Use Shared Auth", command=self.use_shared_auth).grid(row=0, column=5, rowspan=2, padx=5, pady=5, sticky="ns")

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.scope_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(self, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.base_url_var = tk.StringVar(value=PRODUCT_BASE_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        ttk.Label(urls, text="Product Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.base_url_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        controls = ttk.LabelFrame(self, text="Request")
        controls.pack(fill=tk.X, pady=(0, 8))
        self.endpoint_var = tk.StringVar(value="Get Product Status")
        ttk.Combobox(controls, textvariable=self.endpoint_var, values=list(PRODUCT_ENDPOINTS.keys()), state="readonly", width=32).grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ttk.Button(controls, text="Send Request", command=self.send_request).grid(row=0, column=1, padx=5, pady=5)

        self.correlation_id_var = tk.StringVar()
        self.filename_var = tk.StringVar(value="RouteAdvice.pdf")
        self.imo_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.product_name_var = tk.StringVar()
        self.status_var = tk.StringVar(value="All")
        self.customer_ref_var = tk.StringVar()
        self.voyage_info_id_var = tk.StringVar()
        self.route_schedule_id_var = tk.StringVar()

        ttk.Label(controls, text="Correlation ID").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.correlation_id_var).grid(row=1, column=1, columnspan=2, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Filename").grid(row=1, column=3, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.filename_var).grid(row=1, column=4, columnspan=2, sticky="we", padx=5, pady=3)

        ttk.Label(controls, text="IMO").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.imo_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="From").grid(row=2, column=2, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.from_var).grid(row=2, column=3, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="To").grid(row=2, column=4, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.to_var).grid(row=2, column=5, sticky="we", padx=5, pady=3)

        ttk.Label(controls, text="Product").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(controls, textvariable=self.product_name_var, values=["", "RouteAdvice", "RouteComparison"], state="readonly", width=16).grid(row=3, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Status").grid(row=3, column=2, sticky="w", padx=5, pady=3)
        ttk.Combobox(controls, textvariable=self.status_var, values=["All", "New", "Announced", "Sent", "Failed"], state="readonly", width=12).grid(row=3, column=3, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Customer Ref").grid(row=3, column=4, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.customer_ref_var).grid(row=3, column=5, sticky="we", padx=5, pady=3)

        ttk.Label(controls, text="Voyage Info ID").grid(row=4, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.voyage_info_id_var).grid(row=4, column=1, columnspan=2, sticky="we", padx=5, pady=3)
        ttk.Label(controls, text="Schedule ID").grid(row=4, column=3, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.route_schedule_id_var).grid(row=4, column=4, columnspan=2, sticky="we", padx=5, pady=3)

        self.product_url_var = tk.StringVar()
        ttk.Label(controls, text="Product URL").grid(row=5, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(controls, textvariable=self.product_url_var).grid(row=5, column=1, columnspan=4, sticky="we", padx=5, pady=3)
        ttk.Button(controls, text="Download URL", command=self.download_product_url).grid(row=5, column=5, sticky="we", padx=5, pady=3)

        for col in range(6):
            controls.columnconfigure(col, weight=1)

        log_frame = ttk.LabelFrame(self, text="Response / Log")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Save Log", command=self.save_log).pack(side=tk.LEFT, padx=5)

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
                self._remember_auth(data, body)
                self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
                self.log("Token saved in memory.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def send_request(self) -> None:
        def worker() -> None:
            try:
                endpoint = PRODUCT_ENDPOINTS[self.endpoint_var.get()]
                path = self._build_path(endpoint)
                params = self._build_params(endpoint)
                url = self.base_url_var.get().rstrip("/") + path
                self.log(f"GET {url}")
                if params:
                    self.log(f"Query: {json.dumps(params, indent=2)}")
                resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
                self.log(f"HTTP {resp.status_code}")
                self._log_download_or_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Product API Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def download_product_url(self) -> None:
        def worker() -> None:
            try:
                url = self.product_url_var.get().strip()
                if not url:
                    raise ValueError("Product URL is required.")
                self.log(f"GET {url}")
                resp = requests.get(url, headers=self._headers(), timeout=60)
                self.log(f"HTTP {resp.status_code}")
                self._log_download_or_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Product Download Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _build_path(self, endpoint: Dict[str, Any]) -> str:
        path = endpoint["path"]
        if endpoint.get("needs_correlation_id"):
            correlation_id = self.correlation_id_var.get().strip()
            if not correlation_id:
                raise ValueError("Correlation ID is required for this endpoint.")
            path = path.replace("{correlationId}", correlation_id)
        if endpoint.get("needs_filename"):
            filename = self.filename_var.get().strip()
            if not filename:
                raise ValueError("Filename is required for this endpoint.")
            path = path.replace("{filename}", filename)
        return path

    def _build_params(self, endpoint: Dict[str, Any]) -> Dict[str, str]:
        requested = endpoint.get("query", [])
        fields = {
            "imo": self.imo_var,
            "from": self.from_var,
            "to": self.to_var,
            "productName": self.product_name_var,
            "status": self.status_var,
            "customerReferenceId": self.customer_ref_var,
            "voyageInfoId": self.voyage_info_id_var,
            "routeCalculationScheduleId": self.route_schedule_id_var,
        }
        return {name: fields[name].get().strip() for name in requested if fields[name].get().strip()}

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._require_token()}", "Accept": "*/*"}

    def _log_download_or_response(self, resp: requests.Response) -> None:
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type or "problem+json" in content_type:
            self._log_response(resp)
            return
        self.log(f"Content-Type: {content_type or 'unknown'}")
        self.log(f"Downloaded bytes: {len(resp.content)}")
        if resp.ok and resp.content:
            suggested = self.filename_var.get().strip() or "product-download.bin"
            self.after(0, lambda: self._save_download(resp.content, suggested))
        text_preview = resp.text[:2000] if resp.encoding else ""
        if text_preview:
            self.log(text_preview)

    def _save_download(self, content: bytes, suggested: str) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Product",
            initialfile=suggested,
            filetypes=[("All files", "*.*")],
        )
        if path:
            Path(path).write_bytes(content)
            self.log(f"Product saved: {path}")


class NotificationApiFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.access_token: Optional[str] = None
        self.ws: Optional[websocket.WebSocket] = None
        self.stop_ws = threading.Event()
        self._init_log_queue()
        self._build_ui()
        self.after(100, self._flush_log_queue)

    def _build_ui(self) -> None:
        auth = ttk.LabelFrame(self, text="Authentication")
        auth.pack(fill=tk.X, pady=(0, 8))

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.scope_var = tk.StringVar(value=NOTIFICATION_DEFAULT_SCOPE)

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)
        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")
        ttk.Button(auth, text="Use Shared Auth", command=self.use_shared_auth).grid(row=0, column=5, rowspan=2, padx=5, pady=5, sticky="ns")

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.scope_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(self, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.rest_base_var = tk.StringVar(value=NOTIFICATION_REST_BASE_URL)
        self.ws_url_var = tk.StringVar(value=NOTIFICATION_WS_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        ttk.Label(urls, text="REST Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.rest_base_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="WS URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.ws_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        controls = ttk.LabelFrame(self, text="Notification Test")
        controls.pack(fill=tk.X, pady=(0, 8))
        self.auto_reconnect_var = tk.BooleanVar(value=True)
        ttk.Button(controls, text="Health Check", command=self.health_check).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Button(controls, text="Connect WebSocket", command=self.connect_ws).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Button(controls, text="Disconnect", command=self.disconnect_ws).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Checkbutton(controls, text="Auto reconnect", variable=self.auto_reconnect_var).pack(side=tk.LEFT, padx=12, pady=5)

        log_frame = ttk.LabelFrame(self, text="Response / Log")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear Log", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Save Log", command=self.save_log).pack(side=tk.LEFT, padx=5)

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
                self._remember_auth(data, body)
                self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
                self.log("Token saved in memory.")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def health_check(self) -> None:
        def worker() -> None:
            try:
                url = self.rest_base_var.get().rstrip("/") + "/voyage/notification/v1/health-check"
                self.log(f"GET {url}")
                resp = requests.get(url, timeout=30)
                self.log(f"HTTP {resp.status_code}")
                self._log_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Notification API Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def connect_ws(self) -> None:
        def worker() -> None:
            self.stop_ws.clear()
            while not self.stop_ws.is_set():
                try:
                    self.log(f"Opening WebSocket: {self.ws_url_var.get().strip()}")
                    self.ws = websocket.create_connection(
                        self.ws_url_var.get().strip(),
                        header=[f"Authorization: Bearer {self._require_token()}"],
                        timeout=30,
                    )
                    self.log("WebSocket connected. Waiting for product notifications...")
                    while not self.stop_ws.is_set():
                        msg = self.ws.recv()
                        try:
                            self.log(json.dumps(json.loads(msg), indent=2)[:50000])
                        except json.JSONDecodeError:
                            self.log(str(msg)[:50000])
                except Exception as exc:
                    self._close_ws()
                    if self.stop_ws.is_set():
                        break
                    self.log(f"WebSocket disconnected: {exc}")
                    if not self.auto_reconnect_var.get():
                        messagebox.showerror("WebSocket Error", str(exc))
                        break
                    self.log("Refreshing token before reconnect...")
                    try:
                        self._refresh_token_sync()
                    except Exception as token_exc:
                        self.log(f"Token refresh failed: {token_exc}")
                    self.log("Reconnecting in 5 seconds...")
                    self.stop_ws.wait(5)
            self._close_ws()

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_token_sync(self) -> None:
        data = {
            "client_id": self.client_id_var.get().strip(),
            "client_secret": self.client_secret_var.get().strip(),
            "scope": self.scope_var.get().strip(),
            "grant_type": "client_credentials",
        }
        if not data["client_id"] or not data["client_secret"]:
            return
        resp = requests.post(self.token_url_var.get().strip(), data=data, timeout=30)
        self.log(f"Token refresh HTTP {resp.status_code}")
        resp.raise_for_status()
        body = resp.json()
        self._remember_auth(data, body)

    def disconnect_ws(self) -> None:
        self.stop_ws.set()
        self._close_ws()
        self.log("WebSocket disconnect requested.")

    def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


class MapPreviewFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self._build_ui()

    def _build_ui(self) -> None:
        controls = ttk.LabelFrame(self, text="Google Maps Preview")
        controls.pack(fill=tk.X, pady=(0, 8))

        self.api_key_var = tk.StringVar()
        ttk.Label(controls, text="Google Maps API Key").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(controls, textvariable=self.api_key_var, show="*", width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Button(controls, text="Load Active Request", command=self.load_active_request).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(controls, text="Load Active Response", command=self.load_active_response).grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(controls, text="Open Map Preview", command=self.open_map_preview).grid(row=0, column=4, padx=5, pady=5)
        controls.columnconfigure(1, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        source_frame = ttk.LabelFrame(main, text="Map Source JSON")
        result_frame = ttk.LabelFrame(main, text="Extracted Map Data / Log")
        main.add(source_frame, weight=2)
        main.add(result_frame, weight=1)

        self.source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.NONE)
        self.source_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.result_text = scrolledtext.ScrolledText(result_frame, wrap=tk.NONE)
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear", command=self.clear).pack(side=tk.LEFT)

    def clear(self) -> None:
        self.source_text.delete("1.0", tk.END)
        self.result_text.delete("1.0", tk.END)

    def load_active_request(self) -> None:
        frame = self._active_api_frame()
        if frame is None or not hasattr(frame, "request_text"):
            messagebox.showinfo("Map Preview", "The last active API tab does not have request JSON.")
            return
        raw = frame.request_text.get("1.0", tk.END).strip()
        self._set_source(raw, "Loaded request JSON from active API tab.")

    def load_active_response(self) -> None:
        frame = self._active_api_frame()
        if frame is None or not hasattr(frame, "latest_response_data"):
            messagebox.showinfo("Map Preview", "No parsed JSON response is available from the last active API tab.")
            return
        raw = json.dumps(frame.latest_response_data, indent=2)
        self._set_source(raw, "Loaded last JSON response from active API tab.")

    def open_map_preview(self) -> None:
        try:
            data = json.loads(self.source_text.get("1.0", tk.END).strip())
            points, lines = self._extract_map_data(data)
            if not points and not lines:
                raise ValueError("No longitude/latitude coordinates were found.")
            html = self._build_google_maps_html(points, lines, self.api_key_var.get().strip())
            path = Path.cwd() / "abb_map_preview.html"
            path.write_text(html, encoding="utf-8")
            self._log(f"Points: {len(points)}")
            self._log(f"Lines: {len(lines)}")
            self._log(f"Map preview written: {path}")
            webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            messagebox.showerror("Map Preview Error", str(exc))

    def _active_api_frame(self) -> Optional[tk.Misc]:
        root = self.winfo_toplevel()
        return getattr(root, "last_api_tab", None)

    def _set_source(self, raw: str, message: str) -> None:
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", raw)
        self._log(message)

    def _log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.result_text.insert(tk.END, f"[{ts}] {message}\n")
        self.result_text.see(tk.END)

    def _extract_map_data(self, data: Any) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, float]]]]:
        points: List[Dict[str, Any]] = []
        lines: List[List[Dict[str, float]]] = []
        seen_points = set()
        seen_lines = set()

        def coord_pair(value: Any) -> Optional[Dict[str, float]]:
            if not isinstance(value, list) or len(value) < 2:
                return None
            lon, lat = value[0], value[1]
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                return None
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                return None
            return {"lat": float(lat), "lng": float(lon)}

        def add_point(point: Dict[str, float], label: str) -> None:
            key = (round(point["lat"], 7), round(point["lng"], 7), label)
            if key not in seen_points:
                seen_points.add(key)
                points.append({**point, "label": label})

        def add_line(raw_coords: Any) -> None:
            line = [point for item in raw_coords if (point := coord_pair(item))]
            if len(line) < 2:
                return
            key = tuple((round(point["lat"], 7), round(point["lng"], 7)) for point in line)
            if key not in seen_lines:
                seen_lines.add(key)
                lines.append(line)

        def visit(obj: Any, label: str = "Point") -> None:
            if isinstance(obj, dict):
                geom = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else obj
                geom_type = geom.get("type") if isinstance(geom, dict) else None
                coords = geom.get("coordinates") if isinstance(geom, dict) else None

                if geom_type == "Point":
                    point = coord_pair(coords)
                    if point:
                        props = obj.get("properties", {}) if isinstance(obj.get("properties"), dict) else {}
                        add_point(point, str(props.get("name") or label))
                elif geom_type == "LineString":
                    add_line(coords)
                elif geom_type in {"MultiLineString", "Polygon"} and isinstance(coords, list):
                    for item in coords:
                        add_line(item)
                elif geom_type == "MultiPolygon" and isinstance(coords, list):
                    for polygon in coords:
                        for item in polygon:
                            add_line(item)

                if "longitude" in obj and "latitude" in obj:
                    point = coord_pair([obj["longitude"], obj["latitude"]])
                    if point:
                        add_point(point, str(obj.get("name") or label))

                for key, value in obj.items():
                    visit(value, str(key))
            elif isinstance(obj, list):
                if len(obj) > 1 and all(coord_pair(item) for item in obj):
                    add_line(obj)
                else:
                    point = coord_pair(obj)
                    if point:
                        add_point(point, label)
                    for item in obj:
                        visit(item, label)

        visit(data)
        return points, lines

    def _build_google_maps_html(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        api_key: str,
    ) -> str:
        center = points[0] if points else lines[0][0]
        map_data = json.dumps({"points": points, "lines": lines})
        maps_query = f"?key={api_key}&callback=initMap" if api_key else "?callback=initMap"
        no_key_message = (
            "<p class='notice'>Google Maps API key is empty. Add a key in the GUI for full map rendering. "
            "Coordinate links below still open in Google Maps.</p>"
            if not api_key
            else ""
        )
        links = "\n".join(
            f"<li><a target='_blank' href='https://www.google.com/maps/search/?api=1&query={point['lat']},{point['lng']}'>"
            f"{point.get('label', 'Point')} ({point['lat']:.6f}, {point['lng']:.6f})</a></li>"
            for point in points[:50]
        )
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>ABB API Map Preview</title>
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    #panel {{ position: absolute; top: 12px; left: 12px; max-width: 360px; max-height: 45vh; overflow: auto; background: white; padding: 12px; box-shadow: 0 2px 12px #777; font-family: Arial, sans-serif; z-index: 10; }}
    .notice {{ color: #8a4b00; font-weight: 600; }}
  </style>
</head>
<body>
  <div id="panel">
    <strong>ABB API Map Preview</strong>
    {no_key_message}
    <div>Points: {len(points)} / Lines: {len(lines)}</div>
    <ul>{links}</ul>
  </div>
  <div id="map"></div>
  <script>
    const data = {map_data};
    function initMap() {{
      const map = new google.maps.Map(document.getElementById("map"), {{
        zoom: 4,
        center: {{lat: {center["lat"]}, lng: {center["lng"]}}},
        mapTypeId: "terrain"
      }});
      const bounds = new google.maps.LatLngBounds();
      data.points.forEach((point, index) => {{
        const pos = {{lat: point.lat, lng: point.lng}};
        new google.maps.Marker({{position: pos, map, label: String((index % 9) + 1), title: point.label || "Point"}});
        bounds.extend(pos);
      }});
      data.lines.forEach((line) => {{
        const path = line.map((point) => ({{lat: point.lat, lng: point.lng}}));
        new google.maps.Polyline({{path, map, geodesic: true, strokeColor: "#0b57d0", strokeOpacity: 0.9, strokeWeight: 3}});
        path.forEach((pos) => bounds.extend(pos));
      }});
      if (!bounds.isEmpty()) map.fitBounds(bounds);
    }}
  </script>
  <script async defer src="https://maps.googleapis.com/maps/api/js{maps_query}"></script>
</body>
</html>
"""


class AbbApiGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ABB API GUI Tester")
        self.geometry("1240x880")
        self.shared_auth: Dict[str, Any] = {}
        self.last_api_tab: Optional[tk.Misc] = None

        toolbar = ttk.Frame(self, padding=(10, 8, 10, 0))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Environment").pack(side=tk.LEFT)
        self.environment_var = tk.StringVar(value="dev")
        env_box = ttk.Combobox(
            toolbar,
            textvariable=self.environment_var,
            values=list(ENVIRONMENT_URLS.keys()),
            state="readonly",
            width=10,
        )
        env_box.pack(side=tk.LEFT, padx=6)
        env_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_environment())

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self.vessel_tab = VesselRoutingFrame(self.tabs)
        self.voyage_tab = VoyageConfigurationFrame(self.tabs)
        self.product_tab = ProductApiFrame(self.tabs)
        self.notification_tab = NotificationApiFrame(self.tabs)
        self.map_tab = MapPreviewFrame(self.tabs)
        self.api_tabs = {self.vessel_tab, self.voyage_tab, self.product_tab, self.notification_tab}
        self.last_api_tab = self.vessel_tab
        self.tabs.add(self.vessel_tab, text="Vessel Routing API")
        self.tabs.add(self.voyage_tab, text="Voyage Configuration API")
        self.tabs.add(self.product_tab, text="Product API")
        self.tabs.add(self.notification_tab, text="Notification API")
        self.tabs.add(self.map_tab, text="Map Preview")
        self.tabs.bind("<<NotebookTabChanged>>", self._remember_active_api_tab)

    def _remember_active_api_tab(self, _event: tk.Event) -> None:
        selected = self.tabs.nametowidget(self.tabs.select())
        if selected in self.api_tabs:
            self.last_api_tab = selected

    def apply_environment(self) -> None:
        env = ENVIRONMENT_URLS[self.environment_var.get()]
        self.vessel_tab.token_url_var.set(env["token"])
        self.vessel_tab.rest_base_var.set(env["vessel_rest"])
        self.vessel_tab.ws_base_var.set(env["vessel_ws"])
        self.voyage_tab.token_url_var.set(env["token"])
        self.voyage_tab.api_base_var.set(env["voyage_api"])
        self.product_tab.token_url_var.set(env["token"])
        self.product_tab.base_url_var.set(env["product_base"])
        self.notification_tab.token_url_var.set(env["token"])
        self.notification_tab.rest_base_var.set(env["notification_rest"])
        self.notification_tab.ws_url_var.set(env["notification_ws"])


def main() -> None:
    app = AbbApiGui()
    app.mainloop()


if __name__ == "__main__":
    main()
