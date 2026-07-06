"""
ABB Voyage Configuration API GUI Tester

Features
- Get OAuth2 token
- Create, get, update, and search route calculation schedules
- Create route advice and route comparison report requests

Install:
    pip install requests

Run:
    python voyage_configuration_gui.py

Notes
- Base URLs are based on Voyage Configuration API 1.714.
- You need ABB-issued client_id/client_secret and scopes.
- Request JSON samples are intentionally editable before sending.
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


API_BASE_URL = "https://dev.api.voyageoptimization.abb.com"
TOKEN_URL = "https://internal.identity.genix.abilityplatform.abb/public/api/oauth2/token"
DEFAULT_SCOPE = (
    "voyage:route-calculation-schedule:read "
    "voyage:route-calculation-schedule:write "
    "voyage:route-advice:access "
    "voyage:route-comparison:access"
)

ENDPOINTS = {
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

SAMPLES: Dict[str, Dict[str, Any]] = {
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
            {
                "routingCorrelationId": "K4w-THf4DoEERsg=",
                "name": "Route 1",
            },
            {
                "routingCorrelationId": "K4w-THf4DoEERsf=",
                "name": "Route 2",
            },
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


class VoyageConfigurationGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ABB Voyage Configuration API GUI Tester")
        self.geometry("1220x840")

        self.access_token: Optional[str] = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self.after(100, self._flush_log_queue)
        self.load_sample()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        auth = ttk.LabelFrame(root, text="Authentication")
        auth.pack(fill=tk.X, pady=(0, 8))

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.scope_var = tk.StringVar(value=DEFAULT_SCOPE)

        ttk.Label(auth, text="Client ID").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_id_var, width=42).grid(row=0, column=1, sticky="we", padx=5, pady=5)
        ttk.Label(auth, text="Client Secret").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.client_secret_var, width=42, show="*").grid(row=0, column=3, sticky="we", padx=5, pady=5)
        ttk.Button(auth, text="Get Token", command=self.get_token).grid(row=0, column=4, rowspan=2, padx=8, pady=5, sticky="ns")

        ttk.Label(auth, text="Scope").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(auth, textvariable=self.scope_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=5)
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)

        urls = ttk.LabelFrame(root, text="URLs")
        urls.pack(fill=tk.X, pady=(0, 8))
        self.api_base_var = tk.StringVar(value=API_BASE_URL)
        self.token_url_var = tk.StringVar(value=TOKEN_URL)
        ttk.Label(urls, text="API Base").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.api_base_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Label(urls, text="Token URL").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(urls, textvariable=self.token_url_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        urls.columnconfigure(1, weight=1)

        controls = ttk.LabelFrame(root, text="Request")
        controls.pack(fill=tk.X, pady=(0, 8))

        self.endpoint_var = tk.StringVar(value="Create Route Calculation Schedule")
        endpoint_box = ttk.Combobox(
            controls,
            textvariable=self.endpoint_var,
            values=list(ENDPOINTS.keys()),
            state="readonly",
            width=38,
        )
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

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        request_frame = ttk.LabelFrame(main, text="JSON Body")
        response_frame = ttk.LabelFrame(main, text="Response / Log")
        main.add(request_frame, weight=1)
        main.add(response_frame, weight=1)

        self.request_text = scrolledtext.ScrolledText(request_frame, height=28, wrap=tk.NONE)
        self.request_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.log_text = scrolledtext.ScrolledText(response_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(root)
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
                endpoint = ENDPOINTS[self.endpoint_var.get()]
                method = endpoint["method"]
                path = self._build_path(endpoint)
                url = self.api_base_var.get().rstrip("/") + path
                params = self._build_params(endpoint)
                body = self._get_request_json() if endpoint.get("body") else None

                self.log(f"{method} {url}")
                if params:
                    self.log(f"Query: {json.dumps(params, indent=2)}")
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(body is not None),
                    params=params,
                    json=body,
                    timeout=60,
                )
                self.log(f"HTTP {resp.status_code}")
                self._log_rate_limit_headers(resp)
                self._log_response(resp)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Request Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def load_sample(self) -> None:
        endpoint = ENDPOINTS[self.endpoint_var.get()]
        sample_key = endpoint.get("body")
        self.request_text.delete("1.0", tk.END)
        if sample_key:
            self.request_text.insert("1.0", json.dumps(SAMPLES[sample_key], indent=2))
        else:
            self.request_text.insert("1.0", "")

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
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
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

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}\n")

    def _flush_log_queue(self) -> None:
        while not self.log_queue.empty():
            self.log_text.insert(tk.END, self.log_queue.get())
            self.log_text.see(tk.END)
        self.after(100, self._flush_log_queue)


if __name__ == "__main__":
    app = VoyageConfigurationGui()
    app.mainloop()
