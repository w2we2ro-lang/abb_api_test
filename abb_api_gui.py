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
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Tuple

import requests
import websocket

try:
    import tkintermapview
except ImportError:
    tkintermapview = None

try:
    import webview
except ImportError:
    webview = None


def _load_local_defaults() -> Dict[str, str]:
    path = Path(__file__).with_name("abb_gui_defaults.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {key: str(value) for key, value in data.items() if value is not None}


LOCAL_DEFAULTS = _load_local_defaults()

FIXED_ETA_DURATION_DAYS = 14


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _realistic_etd_eta(etd_value: Any = None, eta_value: Any = None) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    minimum_etd = now + timedelta(hours=6)
    etd = _parse_utc(etd_value)
    replaced_etd = etd is None or etd < minimum_etd
    if replaced_etd:
        etd = now + timedelta(days=1)

    eta = None if replaced_etd else _parse_utc(eta_value)
    minimum_eta = etd + timedelta(hours=12)
    maximum_eta = etd + timedelta(days=45)
    if eta is None or eta < minimum_eta or eta > maximum_eta:
        eta = etd + timedelta(days=FIXED_ETA_DURATION_DAYS)
    return _utc_z(etd), _utc_z(eta)


def _future_etd(etd_value: Any = None) -> str:
    now = datetime.now(timezone.utc)
    etd = _parse_utc(etd_value)
    if etd is None or etd < now + timedelta(hours=6):
        etd = now + timedelta(days=1)
    return _utc_z(etd)


def _build_vessel_async_sample(endpoint_name: str) -> Dict[str, Any]:
    sample = VESSEL_ASYNC_SAMPLES.get(endpoint_name, DEFAULT_SHORTEST_PATH_REQUEST)
    payload = json.loads(json.dumps(sample))
    if endpoint_name == "Fixed ETA":
        payload["etd"], payload["eta"] = _realistic_etd_eta(payload.get("etd"), payload.get("eta"))
    elif payload.get("etd"):
        payload["etd"] = _future_etd(payload.get("etd"))
    return payload


TOKEN_URL = os.getenv(
    "ABB_TOKEN_URL",
    LOCAL_DEFAULTS.get("token_url", "https://identity.genix.abilityplatform.abb/public/api/oauth2/token"),
)
DEFAULT_CLIENT_ID = os.getenv("ABB_CLIENT_ID", LOCAL_DEFAULTS.get("client_id", ""))
DEFAULT_CLIENT_SECRET = os.getenv("ABB_CLIENT_SECRET", LOCAL_DEFAULTS.get("client_secret", ""))
DEFAULT_TOKEN_SCOPE = os.getenv(
    "ABB_SCOPE",
    LOCAL_DEFAULTS.get("scope", "https://genb2cep01euwprod.onmicrosoft.com/rs.iam/region"),
)

VESSEL_REST_BASE_URL = "https://api.voyageoptimization.abb.com/vessel-routing/v2"
VESSEL_WS_BASE_URL = "wss://api.voyageoptimization.abb.com/vessel-routing/v2"
VESSEL_DEFAULT_SCOPE = DEFAULT_TOKEN_SCOPE

VOYAGE_API_BASE_URL = "https://api.voyageoptimization.abb.com"
VOYAGE_DEFAULT_SCOPE = DEFAULT_TOKEN_SCOPE
PRODUCT_BASE_URL = "https://api.voyageoptimization.abb.com/voyage/products/v1"
PRODUCT_DEFAULT_SCOPE = DEFAULT_TOKEN_SCOPE
NOTIFICATION_REST_BASE_URL = "https://api.voyageoptimization.abb.com"
NOTIFICATION_WS_URL = "wss://api.voyageoptimization.abb.com/voyage/notification/v1/ws/products"
NOTIFICATION_DEFAULT_SCOPE = DEFAULT_TOKEN_SCOPE

ENVIRONMENT_URLS = {
    "dev": {
        "token": TOKEN_URL,
        "vessel_rest": "https://dev.api.voyageoptimization.abb.com/vessel-routing/v2",
        "vessel_ws": "wss://dev.api.voyageoptimization.abb.com/vessel-routing/v2",
        "voyage_api": "https://dev.api.voyageoptimization.abb.com",
        "product_base": "https://dev.api.voyageoptimization.abb.com/voyage/products/v1",
        "notification_rest": "https://dev.api.voyageoptimization.abb.com",
        "notification_ws": "wss://dev.api.voyageoptimization.abb.com/voyage/notification/v1/ws/products",
    },
    "prod": {
        "token": TOKEN_URL,
        "vessel_rest": VESSEL_REST_BASE_URL,
        "vessel_ws": VESSEL_WS_BASE_URL,
        "voyage_api": VOYAGE_API_BASE_URL,
        "product_base": PRODUCT_BASE_URL,
        "notification_rest": NOTIFICATION_REST_BASE_URL,
        "notification_ws": NOTIFICATION_WS_URL,
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

VESSEL_SAMPLE_VOYAGE = {
    "ports": [
        {
            "type": "Feature",
            "properties": None,
            "geometry": {"type": "Point", "coordinates": [8.577543, 53.535847]},
        },
        {"portId": "NLRTM-2745"},
    ],
}

VESSEL_SAMPLE_PARAMETERS = {
    "vesselName": "Sample Vessel",
    "imo": "8814275",
    "vesselType": "DryBulkCarrier",
    "cargo": {
        "loadCondition": "Loaded",
        "loadState": "Packaged",
        "dangerousCargo": ["DangerousGoods"],
    },
    "measurements": {
        "lengthOverall": 100,
        "beam": 20,
        "draft": {"aft": 10, "fore": 10},
        "airDraft": 10,
        "grossTonnage": 100000,
        "deadweight": 250000,
    },
    "fuelCurve": {
        "values": [
            {"speed": 8, "fuelUsage": 2.6},
            {"speed": 12, "fuelUsage": 9.2},
        ],
    },
    "otherFuelConsumption": {
        "values": [
            {"speedFrom": 0, "speedTo": 12, "otherFuel": 1},
            {"speedFrom": 12, "speedTo": 50, "otherFuel": 2.5},
        ],
    },
    "rpmCurve": {
        "values": [
            {"speed": 0, "rpm": 0},
            {"speed": 10, "rpm": 1000},
        ],
    },
    "powerCurve": {
        "values": [
            {"speed": 0, "maxContinuousRating": 0},
            {"speed": 10, "maxContinuousRating": 0.9},
        ],
    },
    "safetyMargins": {"port": 0, "starboard": 0, "underKeel": 0, "air": 0, "aft": 0, "forward": 0},
    "cii": {"yearToDateDistance": 10, "yearToDateCo2Emissions": 0},
}

VESSEL_SAMPLE_COSTS_AND_FUEL = {
    "vesselCosts": 25000,
    "nonEcaFuel": {
        "mainEngineFuel": [{"consumptionCurveFactor": 1, "cost": 800, "emissionFactor": 3.114, "fuelType": "HFO"}],
        "otherFuel": [{"consumptionCurveFactor": 1, "cost": 200, "emissionFactor": 3.114, "fuelType": "MGO"}],
    },
    "ecaFuel": {
        "default": {
            "mainEngineFuel": [{"consumptionCurveFactor": 1, "cost": 800, "emissionFactor": 3.206, "fuelType": "MGO"}],
            "otherFuel": [{"consumptionCurveFactor": 1, "cost": 200, "emissionFactor": 3.206, "fuelType": "MGO"}],
        }
    },
}

VESSEL_SAMPLE_CONFIG = {
    "hoursBetweenRouteWaypoints": 6,
    "avoidCoastalAreas": True,
    "followShortestNavigableRoute": False,
    "groundingCheckMode": "Off",
}

VESSEL_ASYNC_SAMPLES: Dict[str, Dict[str, Any]] = {
    "Shortest path": DEFAULT_SHORTEST_PATH_REQUEST,
    "Instructed set speed": {
        "id": "instructed-speed-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
        "config": VESSEL_SAMPLE_CONFIG,
        "speed": 10,
    },
    "Recommended set speed": {
        "id": "recommended-speed-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
        "config": VESSEL_SAMPLE_CONFIG,
        "speeds": [{"minimum": 8, "maximum": 12}],
    },
    "Fixed ETA": {
        "id": "fixed-eta-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "eta": "2026-07-12T12:00:00Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
        "config": VESSEL_SAMPLE_CONFIG,
        "speeds": [{"minimum": 8, "maximum": 12}],
    },
    "Optimal set speed": {
        "id": "optimal-speed-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
        "costsAndFuelInfo": VESSEL_SAMPLE_COSTS_AND_FUEL,
        "config": {**VESSEL_SAMPLE_CONFIG, "minimumHoursBetweenSpeedChanges": 24},
        "speeds": [{"minimum": 8, "maximum": 12}],
        "optimizationType": "Time",
    },
}

RTZ_REQUEST_TYPES = {
    "Shortest Path Request": "Shortest path",
    "Instructed Speed Request": "Instructed set speed",
    "Recommended Speed Request": "Recommended set speed",
    "Fixed ETA Request": "Fixed ETA",
    "Optimal Speed Request": "Optimal set speed",
}

MAP_TILE_PROVIDERS = {
    "Vector Light": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "description": "OpenStreetMap vector-style map (no API key)",
    },
    "Vector Street": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "max_zoom": 19,
        "description": "Esri World Street Map (no API key)",
    },
    "Satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "max_zoom": 19,
        "description": "Esri World Imagery satellite map (no API key)",
    },
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

VOYAGE_FIELD_SAMPLES: Dict[str, Dict[str, str]] = {
    "Create Route Calculation Schedule": {
        "imo": "9876543",
        "from": "2026-07-06T00:00:00Z",
        "to": "2026-07-13T00:00:00Z",
        "status": "Active",
        "customerReferenceId": "123456789",
    },
    "Get Route Calculation Schedule": {
        "routeCalculationScheduleId": "route-schedule-123",
        "revision": "1",
    },
    "Update Route Calculation Schedule": {
        "routeCalculationScheduleId": "route-schedule-123",
        "revision": "1",
    },
    "Search Route Calculation Schedules": {
        "imo": "9876543",
        "from": "2026-07-06T00:00:00Z",
        "to": "2026-07-13T00:00:00Z",
        "status": "Active",
        "customerReferenceId": "123456789",
        "routeCalculationScheduleId": "route-schedule-123",
    },
    "Create Route Advice Report": {
        "customerReferenceId": "123456789",
    },
    "Create Route Comparison Report": {
        "customerReferenceId": "123456789",
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

PRODUCT_FIELD_SAMPLES: Dict[str, Dict[str, str]] = {
    "Get Product Status": {
        "correlationId": "K4w-THf4DoEERsg=",
    },
    "Download Product": {
        "correlationId": "K4w-THf4DoEERsg=",
        "filename": "RouteAdvice.pdf",
        "productUrl": "https://dev.api.voyageoptimization.abb.com/voyage/products/v1/reports/K4w-THf4DoEERsg=/RouteAdvice.pdf",
    },
    "Get Last Sent Product": {
        "imo": "9876543",
        "productName": "RouteAdvice",
        "customerReferenceId": "123456789",
    },
    "Search Products": {
        "imo": "9876543",
        "from": "2026-07-06T00:00:00Z",
        "to": "2026-07-13T00:00:00Z",
        "productName": "RouteAdvice",
        "status": "All",
        "customerReferenceId": "123456789",
        "voyageInfoId": "ABC-123",
        "routeCalculationScheduleId": "route-schedule-123",
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

    def _token_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _raise_for_token_response(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        content_type = resp.headers.get("Content-Type", "")
        self.log(f"Token error Content-Type: {content_type or 'unknown'}")
        try:
            self.log("Token error response body:")
            self.log(json.dumps(resp.json(), indent=2)[:20000])
        except Exception:
            body = resp.text.strip()
            self.log(body[:20000] if body else "<empty response body>")
        resp.raise_for_status()

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

        self.client_id_var = tk.StringVar(value=DEFAULT_CLIENT_ID)
        self.client_secret_var = tk.StringVar(value=DEFAULT_CLIENT_SECRET)
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
        async_box = ttk.Combobox(async_frame, textvariable=self.async_endpoint_var, values=list(VESSEL_ASYNC_ENDPOINTS.keys()), state="readonly", width=28)
        async_box.grid(row=0, column=0, padx=5, pady=5)
        async_box.bind("<<ComboboxSelected>>", lambda _event: self.load_async_sample())
        ttk.Button(async_frame, text="Send WebSocket Request", command=self.send_ws_request).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(async_frame, text="Client ID for WS response routing").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.ws_client_id_var = tk.StringVar(value="test-client-001")
        ttk.Entry(async_frame, textvariable=self.ws_client_id_var, width=34).grid(row=1, column=1, sticky="we", padx=5, pady=5)
        async_frame.columnconfigure(1, weight=1)

        rtz_frame = ttk.LabelFrame(left, text="RTZ Converter")
        rtz_frame.pack(fill=tk.X, pady=(0, 8))
        self.rtz_path_var = tk.StringVar()
        self.rtz_request_type_var = tk.StringVar(value="Shortest Path Request")
        ttk.Entry(rtz_frame, textvariable=self.rtz_path_var).grid(row=0, column=0, sticky="we", padx=5, pady=5)
        ttk.Button(rtz_frame, text="Browse RTZ", command=self.browse_rtz_file).grid(row=0, column=1, padx=5, pady=5)
        ttk.Combobox(
            rtz_frame,
            textvariable=self.rtz_request_type_var,
            values=list(RTZ_REQUEST_TYPES.keys()),
            state="readonly",
            width=28,
        ).grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Button(rtz_frame, text="Convert RTZ", command=self.convert_rtz_to_request).grid(row=1, column=1, padx=5, pady=5)
        rtz_frame.columnconfigure(0, weight=1)

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
        ttk.Button(bottom, text="Load Async Sample", command=self.load_async_sample).pack(side=tk.LEFT, padx=5)

    def load_async_sample(self) -> None:
        sample = _build_vessel_async_sample(self.async_endpoint_var.get())
        self.request_text.delete("1.0", tk.END)
        self.request_text.insert("1.0", json.dumps(sample, indent=2))

    def browse_rtz_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select RTZ Route",
            filetypes=[("RTZ files", "*.rtz"), ("XML files", "*.xml"), ("All files", "*.*")],
        )
        if path:
            self.rtz_path_var.set(path)

    def convert_rtz_to_request(self) -> None:
        try:
            path = self.rtz_path_var.get().strip()
            if not path:
                raise ValueError("Select an RTZ file first.")
            points, schedule = self._parse_rtz(Path(path))
            if len(points) < 2:
                raise ValueError("RTZ route must contain at least two waypoints.")
            request_type = self.rtz_request_type_var.get()
            endpoint_name = RTZ_REQUEST_TYPES[request_type]
            payload = self._build_rtz_request(endpoint_name, points, schedule)
            self.async_endpoint_var.set(endpoint_name)
            self.request_text.delete("1.0", tk.END)
            self.request_text.insert("1.0", json.dumps(payload, indent=2))
            self.log(f"RTZ converted: {len(points)} waypoints -> {request_type}")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            messagebox.showerror("RTZ Conversion Error", str(exc))

    def _parse_rtz(self, path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        root = ET.parse(path).getroot()
        namespace = {"rtz": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        waypoint_path = ".//rtz:waypoint" if namespace else ".//waypoint"
        position_path = "rtz:position" if namespace else "position"
        schedule_path = ".//rtz:scheduleElement" if namespace else ".//scheduleElement"

        schedule_by_waypoint: Dict[str, Dict[str, Any]] = {}
        speeds = []
        etd = None
        eta = None
        for item in root.findall(schedule_path, namespace):
            waypoint_id = item.attrib.get("waypointId")
            waypoint_schedule: Dict[str, Any] = {}
            if item.attrib.get("speed"):
                speed = float(item.attrib["speed"])
                speeds.append(speed)
                waypoint_schedule["speed"] = speed
            if item.attrib.get("etd"):
                waypoint_schedule["etd"] = item.attrib["etd"]
            if item.attrib.get("eta"):
                waypoint_schedule["eta"] = item.attrib["eta"]
            if waypoint_id:
                schedule_by_waypoint[waypoint_id] = waypoint_schedule
            etd = etd or item.attrib.get("etd")
            eta = item.attrib.get("eta") or eta

        points = []
        for waypoint in root.findall(waypoint_path, namespace):
            position = waypoint.find(position_path, namespace)
            if position is None:
                continue
            lat = float(position.attrib["lat"])
            lon = float(position.attrib["lon"])
            waypoint_id = waypoint.attrib.get("id")
            name = waypoint.attrib.get("name") or f"WP {waypoint_id or len(points)}"
            geometry_type = (waypoint.find("rtz:leg", namespace) if namespace else waypoint.find("leg"))
            force_rhumb_line = geometry_type is not None and geometry_type.attrib.get("geometryType") == "Loxodrome"
            properties = {"name": name, "forceRhumbLine": force_rhumb_line}
            if waypoint_id in schedule_by_waypoint:
                properties.update(schedule_by_waypoint[waypoint_id])
            points.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

        return points, {"speeds": speeds, "etd": etd, "eta": eta}

    def _build_rtz_request(
        self,
        endpoint_name: str,
        points: List[Dict[str, Any]],
        schedule: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = _build_vessel_async_sample(endpoint_name)
        payload["id"] = f"rtz-{endpoint_name.lower().replace(' ', '-')}"
        payload["points"] = points
        payload["voyage"] = {"ports": points}
        if schedule.get("etd") and endpoint_name != "Fixed ETA":
            payload["etd"] = _future_etd(schedule["etd"])

        speeds = schedule.get("speeds") or []
        average_speed = round(sum(speeds) / len(speeds), 2) if speeds else 10
        if endpoint_name == "Instructed set speed":
            payload["speed"] = average_speed
        elif endpoint_name in {"Recommended set speed", "Optimal set speed"}:
            payload["speeds"] = [{"minimum": max(1, round(average_speed - 2, 2)), "maximum": round(average_speed + 2, 2)}]
        elif endpoint_name == "Fixed ETA":
            payload["etd"], payload["eta"] = _realistic_etd_eta(schedule.get("etd"), schedule.get("eta"))
            payload["speeds"] = [{"minimum": max(1, round(average_speed - 2, 2)), "maximum": round(average_speed + 2, 2)}]
        return payload

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
                resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
                self.log(f"Token response HTTP {resp.status_code}")
                self._raise_for_token_response(resp)
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
                ws_messages = []

                while True:
                    msg = ws.recv()
                    self.log("WebSocket message received:")
                    try:
                        parsed = json.loads(msg)
                        ws_messages.append(parsed)
                        self.latest_response_data = {
                            "webSocketMessages": ws_messages,
                            "latestMessage": parsed,
                        }
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

        self.client_id_var = tk.StringVar(value=DEFAULT_CLIENT_ID)
        self.client_secret_var = tk.StringVar(value=DEFAULT_CLIENT_SECRET)
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
                resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
                self.log(f"Token response HTTP {resp.status_code}")
                self._log_rate_limit_headers(resp)
                self._raise_for_token_response(resp)
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
        endpoint_name = self.endpoint_var.get()
        self._load_field_sample(endpoint_name)
        endpoint = VOYAGE_ENDPOINTS[endpoint_name]
        sample_key = endpoint.get("body")
        self.request_text.delete("1.0", tk.END)
        if sample_key:
            self.request_text.insert("1.0", json.dumps(VOYAGE_SAMPLES[sample_key], indent=2))

    def _load_field_sample(self, endpoint_name: str) -> None:
        sample = VOYAGE_FIELD_SAMPLES.get(endpoint_name, {})
        fields = {
            "routeCalculationScheduleId": self.schedule_id_var,
            "revision": self.revision_var,
            "imo": self.imo_var,
            "from": self.from_var,
            "to": self.to_var,
            "status": self.status_var,
            "customerReferenceId": self.customer_ref_var,
            "searchRouteCalculationScheduleId": self.search_schedule_id_var,
        }
        for key, var in fields.items():
            source_key = "routeCalculationScheduleId" if key == "searchRouteCalculationScheduleId" else key
            var.set(sample.get(source_key, ""))

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

        self.client_id_var = tk.StringVar(value=DEFAULT_CLIENT_ID)
        self.client_secret_var = tk.StringVar(value=DEFAULT_CLIENT_SECRET)
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
        endpoint_box = ttk.Combobox(controls, textvariable=self.endpoint_var, values=list(PRODUCT_ENDPOINTS.keys()), state="readonly", width=32)
        endpoint_box.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        endpoint_box.bind("<<ComboboxSelected>>", lambda _event: self.load_sample())
        ttk.Button(controls, text="Send Request", command=self.send_request).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(controls, text="Load Sample", command=self.load_sample).grid(row=0, column=2, padx=5, pady=5)

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
        self.load_sample()

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
                resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
                self.log(f"Token response HTTP {resp.status_code}")
                self._raise_for_token_response(resp)
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

    def load_sample(self) -> None:
        sample = PRODUCT_FIELD_SAMPLES.get(self.endpoint_var.get(), {})
        fields = {
            "correlationId": self.correlation_id_var,
            "filename": self.filename_var,
            "imo": self.imo_var,
            "from": self.from_var,
            "to": self.to_var,
            "productName": self.product_name_var,
            "status": self.status_var,
            "customerReferenceId": self.customer_ref_var,
            "voyageInfoId": self.voyage_info_id_var,
            "routeCalculationScheduleId": self.route_schedule_id_var,
            "productUrl": self.product_url_var,
        }
        for key, var in fields.items():
            var.set(sample.get(key, ""))

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

        self.client_id_var = tk.StringVar(value=DEFAULT_CLIENT_ID)
        self.client_secret_var = tk.StringVar(value=DEFAULT_CLIENT_SECRET)
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
                resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
                self.log(f"Token response HTTP {resp.status_code}")
                self._raise_for_token_response(resp)
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
                    ws_messages = []
                    while not self.stop_ws.is_set():
                        msg = self.ws.recv()
                        try:
                            parsed = json.loads(msg)
                            ws_messages.append(parsed)
                            if len(ws_messages) > 100:
                                ws_messages = ws_messages[-100:]
                            self.latest_response_data = {
                                "webSocketMessages": ws_messages,
                                "latestMessage": parsed,
                            }
                            self.log(json.dumps(parsed, indent=2)[:50000])
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
        resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
        self.log(f"Token refresh HTTP {resp.status_code}")
        self._raise_for_token_response(resp)
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
        self.source_data: Optional[Any] = None
        self._last_extract_stats: Dict[str, int] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        controls = ttk.LabelFrame(self, text="Embedded Vector Map Preview")
        controls.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(controls, text="Provider").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.map_provider_var = tk.StringVar(value="Vector Light")
        provider_box = ttk.Combobox(
            controls,
            textvariable=self.map_provider_var,
            values=list(MAP_TILE_PROVIDERS.keys()),
            state="readonly",
            width=18,
        )
        provider_box.grid(row=0, column=1, sticky="w", padx=5, pady=5)
        provider_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_map_provider())
        self.map_provider_label_var = tk.StringVar(value=MAP_TILE_PROVIDERS["Vector Light"]["description"])
        ttk.Label(controls, textvariable=self.map_provider_label_var).grid(row=0, column=2, sticky="w", padx=5, pady=5)
        ttk.Button(controls, text="Load Active Request", command=self.load_active_request).grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(controls, text="Load Active Response", command=self.load_active_response).grid(row=0, column=4, padx=5, pady=5)
        ttk.Button(controls, text="Show In App", command=self.show_map_preview).grid(row=0, column=5, padx=5, pady=5)
        ttk.Button(controls, text="Open 3D Globe", command=self.show_globe_preview).grid(row=0, column=6, padx=5, pady=5)
        ttk.Label(controls, text="Max markers").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.max_markers_var = tk.StringVar(value="1000")
        ttk.Spinbox(controls, textvariable=self.max_markers_var, from_=100, to=20000, increment=100, width=10).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        controls.columnconfigure(1, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        source_frame = ttk.LabelFrame(main, text="Map Source JSON")
        right_frame = ttk.Frame(main)
        main.add(source_frame, weight=2)
        main.add(right_frame, weight=3)

        self.source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.NONE)
        self.source_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        map_frame = ttk.LabelFrame(right_frame, text="Vector Map")
        map_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.map_widget = None
        if tkintermapview is None:
            ttk.Label(
                map_frame,
                text="Install tkintermapview from requirements_abb_gui.txt to show the embedded map.",
            ).pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        else:
            self.map_widget = tkintermapview.TkinterMapView(map_frame, corner_radius=0)
            self.map_widget.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            self.apply_map_provider()
            self.map_widget.set_position(29.7262421, -95.2641144)
            self.map_widget.set_zoom(3)

        result_frame = ttk.LabelFrame(right_frame, text="Extracted Map Data / Log")
        result_frame.pack(fill=tk.BOTH, expand=False)
        self.result_text = scrolledtext.ScrolledText(result_frame, wrap=tk.NONE)
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear", command=self.clear).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Reset Map", command=self.reset_map).pack(side=tk.LEFT, padx=5)

    def apply_map_provider(self) -> None:
        provider = MAP_TILE_PROVIDERS.get(self.map_provider_var.get(), MAP_TILE_PROVIDERS["Vector Light"])
        self.map_provider_label_var.set(provider["description"])
        if self.map_widget is not None:
            self.map_widget.set_tile_server(provider["url"], max_zoom=provider["max_zoom"])

    def clear(self) -> None:
        self.source_data = None
        self.source_text.delete("1.0", tk.END)
        self.result_text.delete("1.0", tk.END)
        self.reset_map()

    def reset_map(self) -> None:
        if self.map_widget is None:
            return
        self.map_widget.delete_all_marker()
        self.map_widget.delete_all_path()
        self.map_widget.set_position(29.7262421, -95.2641144)
        self.map_widget.set_zoom(3)

    def load_active_request(self) -> None:
        frame = self._active_api_frame()
        if frame is None or not hasattr(frame, "request_text"):
            messagebox.showinfo("Map Preview", "The last active API tab does not have request JSON.")
            return
        raw = frame.request_text.get("1.0", tk.END).strip()
        self.source_data = None
        self._set_source(raw, "Loaded request JSON from active API tab.")

    def load_active_response(self) -> None:
        frame = self._active_api_frame()
        if frame is None or not hasattr(frame, "latest_response_data"):
            messagebox.showinfo("Map Preview", "No parsed JSON response is available from the last active API tab.")
            return
        self.source_data = frame.latest_response_data
        preview = json.dumps(self.source_data, indent=2)[:50000]
        if len(preview) == 50000:
            preview += "\n\n... preview truncated; full response is kept in memory for map rendering ..."
        self._set_source(preview, "Loaded last JSON response from active API tab.")

    def show_map_preview(self) -> None:
        try:
            data = self.source_data if self.source_data is not None else json.loads(self.source_text.get("1.0", tk.END).strip())
            data = self._expand_download_urls(data)
            max_markers = self._get_max_markers()
            points, lines, routes = self._extract_map_data(data, max_points=max_markers)
            if not points and not lines and not routes:
                raise ValueError("No longitude/latitude coordinates were found.")
            self._render_embedded_map(points, lines, routes)
            stats = self._last_extract_stats
            self._log(f"Points rendered: {len(points)} / found: {stats.get('points_found', len(points))}")
            if stats.get("points_sampled", 0):
                self._log(f"Large point set sampled to max markers: {max_markers}")
            self._log(f"Lines: {len(lines)}")
            self._log(f"Routes: {len(routes)} / route nodes: {stats.get('route_nodes_found', 0)}")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            messagebox.showerror("Map Preview Error", str(exc))

    def show_globe_preview(self) -> None:
        try:
            data = self.source_data if self.source_data is not None else json.loads(self.source_text.get("1.0", tk.END).strip())
            data = self._expand_download_urls(data)
            max_markers = self._get_max_markers()
            points, lines, routes = self._extract_map_data(data, max_points=max_markers, max_line_points=20000)
            if not points and not lines and not routes:
                raise ValueError("No longitude/latitude coordinates were found.")
            html_path = self._write_globe_html(points, lines, routes)
            self._open_globe_preview(html_path)
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            messagebox.showerror("3D Globe Error", str(exc))

    def _open_globe_preview(self, html_path: Path) -> None:
        if webview is None:
            webbrowser.open(html_path.as_uri())
            self._log(f"3D globe opened in browser fallback: {html_path}")
            return

        def worker() -> None:
            try:
                webview.create_window("ABB 3D Globe Preview", html_path.as_uri(), width=1280, height=820)
                webview.start()
            except Exception:
                webbrowser.open(html_path.as_uri())

        threading.Thread(target=worker, daemon=True).start()
        self._log(f"3D globe opened in app window: {html_path}")

    def _expand_download_urls(self, data: Any, max_downloads: int = 5) -> Any:
        urls = self._find_download_urls(data)
        if not urls:
            return data

        downloaded = []
        for index, url in enumerate(urls[:max_downloads], start=1):
            try:
                self._log(f"Fetching route response JSON {index}/{min(len(urls), max_downloads)}...")
                resp = requests.get(url, timeout=60)
                self._log(f"Route response HTTP {resp.status_code}")
                resp.raise_for_status()
                downloaded.append(resp.json())
            except Exception as exc:
                self._log(f"Route response download failed: {exc}")

        if not downloaded:
            return data

        if isinstance(data, dict):
            expanded = dict(data)
            expanded["downloadedResponses"] = downloaded
            self.source_data = expanded
            return expanded
        return {"source": data, "downloadedResponses": downloaded}

    def _find_download_urls(self, data: Any) -> List[str]:
        urls: List[str] = []
        seen = set()

        def visit(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in {"downloadUrl", "responseUrl"} and isinstance(value, str) and value.startswith(("http://", "https://")):
                        if value not in seen:
                            seen.add(value)
                            urls.append(value)
                    else:
                        visit(value)
            elif isinstance(obj, list):
                for item in obj:
                    visit(item)

        visit(data)
        return urls

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

    def _get_max_markers(self) -> int:
        try:
            value = int(self.max_markers_var.get())
        except ValueError:
            value = 1000
        return max(100, min(value, 20000))

    def _extract_map_data(
        self,
        data: Any,
        max_points: int = 1000,
        max_line_points: int = 5000,
    ) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, float]]], List[Dict[str, Any]]]:
        points: List[Dict[str, Any]] = []
        lines: List[List[Dict[str, float]]] = []
        routes: List[Dict[str, Any]] = []
        seen_points = set()
        seen_lines = set()
        seen_routes = set()
        stats = {"points_found": 0, "points_sampled": 0, "line_points_trimmed": 0, "route_nodes_found": 0}

        def coord_pair(value: Any) -> Optional[Dict[str, float]]:
            if not isinstance(value, list) or len(value) < 2:
                return None
            lon, lat = value[0], value[1]
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                return None
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                return None
            return {"lat": float(lat), "lng": float(lon)}

        def feature_point(value: Any) -> Optional[Dict[str, float]]:
            if not isinstance(value, dict):
                return None
            geometry = value.get("geometry")
            if not isinstance(geometry, dict) or geometry.get("type") != "Point":
                return None
            return coord_pair(geometry.get("coordinates"))

        def numeric_value(value: Any) -> Optional[float]:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    return None
            return None

        def extract_speed(value: Any) -> Optional[float]:
            if not isinstance(value, dict):
                return None
            for key in ("speed", "speedOverGround", "speedKnots", "sog", "plannedSpeed"):
                speed = numeric_value(value.get(key))
                if speed is not None:
                    return speed
            properties = value.get("properties")
            if isinstance(properties, dict):
                speed = extract_speed(properties)
                if speed is not None:
                    return speed
            speed_ranges = value.get("speeds")
            if isinstance(speed_ranges, list) and speed_ranges:
                first_range = speed_ranges[0]
                if isinstance(first_range, dict):
                    minimum = numeric_value(first_range.get("minimum"))
                    maximum = numeric_value(first_range.get("maximum"))
                    if minimum is not None and maximum is not None:
                        return round((minimum + maximum) / 2, 2)
                    return minimum if minimum is not None else maximum
            return None

        def add_point(point: Dict[str, float], label: str) -> None:
            stats["points_found"] += 1
            key = (round(point["lat"], 7), round(point["lng"], 7), label)
            if key in seen_points:
                return
            seen_points.add(key)
            item = {**point, "label": label}
            if len(points) < max_points:
                points.append(item)
            else:
                stats["points_sampled"] += 1
                points[(stats["points_found"] - 1) % max_points] = item

        def add_line(raw_coords: Any) -> None:
            line = [point for item in raw_coords if (point := coord_pair(item))]
            if len(line) < 2:
                return
            if len(line) > max_line_points:
                step = max(1, len(line) // max_line_points)
                line = line[::step][:max_line_points]
                stats["line_points_trimmed"] += 1
            key = tuple((round(point["lat"], 7), round(point["lng"], 7)) for point in line)
            if key not in seen_lines:
                seen_lines.add(key)
                lines.append(line)

        def add_feature_point_route(features: Any, parent: Dict[str, Any], label: str) -> bool:
            if not isinstance(features, list):
                return False
            route_points = []
            parent_speed = extract_speed(parent)
            for index, item in enumerate(features, start=1):
                point = feature_point(item)
                if not point:
                    continue
                props = item.get("properties", {}) if isinstance(item, dict) and isinstance(item.get("properties"), dict) else {}
                item_name = item.get("name") if isinstance(item, dict) else None
                point_label = str(props.get("name") or item_name or f"WP {index}")
                point_speed = extract_speed(item)
                route_points.append({**point, "label": point_label, "speed": point_speed if point_speed is not None else parent_speed})
            if len(route_points) < 2:
                return False
            stats["route_nodes_found"] += len(route_points)
            if len(route_points) > max_line_points:
                step = max(1, len(route_points) // max_line_points)
                route_points = route_points[::step][:max_line_points]
                stats["line_points_trimmed"] += 1
            key = tuple((round(point["lat"], 7), round(point["lng"], 7)) for point in route_points)
            if key in seen_routes:
                return True
            seen_routes.add(key)
            route_label = str(parent.get("type") or parent.get("id") or parent.get("requestId") or label or "Route")
            routes.append({"label": route_label, "points": route_points})
            return True

        def visit(obj: Any, label: str = "Point") -> None:
            if isinstance(obj, dict):
                geom = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else obj
                geom_type = geom.get("type") if isinstance(geom, dict) else None
                coords = geom.get("coordinates") if isinstance(geom, dict) else None
                handled_geometry = False

                # ABB Path/Route responses return route geometry as paths[].points[] or routes[].points[],
                # where each item is a GeoJSON Point feature. Render them as route nodes instead of port markers.
                handled_route_points = False
                if isinstance(obj.get("points"), list):
                    handled_route_points = add_feature_point_route(obj["points"], obj, label)

                if geom_type == "Point":
                    point = coord_pair(coords)
                    if point:
                        props = obj.get("properties", {}) if isinstance(obj.get("properties"), dict) else {}
                        add_point(point, str(props.get("name") or label))
                        handled_geometry = True
                elif geom_type == "LineString":
                    add_line(coords)
                    handled_geometry = True
                elif geom_type in {"MultiLineString", "Polygon"} and isinstance(coords, list):
                    for item in coords:
                        add_line(item)
                    handled_geometry = True
                elif geom_type == "MultiPolygon" and isinstance(coords, list):
                    for polygon in coords:
                        for item in polygon:
                            add_line(item)
                    handled_geometry = True

                if "longitude" in obj and "latitude" in obj:
                    point = coord_pair([obj["longitude"], obj["latitude"]])
                    if point:
                        add_point(point, str(obj.get("name") or label))

                for key, value in obj.items():
                    if handled_route_points and key == "points":
                        continue
                    if handled_geometry and key in {"geometry", "coordinates"}:
                        continue
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
        self._last_extract_stats = stats
        return points, lines, routes

    def _render_embedded_map(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        routes: List[Dict[str, Any]],
    ) -> None:
        if self.map_widget is None:
            raise RuntimeError("tkintermapview is not installed. Install requirements_abb_gui.txt and restart the GUI.")

        self.map_widget.delete_all_marker()
        self.map_widget.delete_all_path()
        positions: List[Tuple[float, float]] = []

        for index, point in enumerate(points, start=1):
            lat = point["lat"]
            lng = point["lng"]
            positions.append((lat, lng))
            self.map_widget.set_marker(lat, lng, text=f"{index}. {point.get('label', 'Point')}")

        for line in lines:
            path = [(point["lat"], point["lng"]) for point in line]
            if len(path) >= 2:
                positions.extend(path)
                for segment in self._split_antimeridian_path(path):
                    self.map_widget.set_path(segment, color="#00d4ff", width=3)

        for route_index, route in enumerate(routes, start=1):
            route_points = route.get("points", [])
            if len(route_points) < 2:
                continue
            positions.extend((point["lat"], point["lng"]) for point in route_points)
            self._render_route_path(route_points)
            self._render_route_nodes(route_index, route_points)
            self._log_speed_profile(route_index, str(route.get("label") or "Route"), route_points)

        if positions:
            self._fit_positions(positions)

    def _write_globe_html(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        routes: List[Dict[str, Any]],
    ) -> Path:
        payload = {"points": points, "lines": lines, "routes": routes}
        payload_json = json.dumps(payload)
        html = self._globe_html_template().replace("__MAP_PAYLOAD__", payload_json)
        output_path = Path(tempfile.gettempdir()) / "abb_api_3d_globe_preview.html"
        output_path.write_text(html, encoding="utf-8")
        return output_path

    def _globe_html_template(self) -> str:
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ABB API 3D Globe Preview</title>
  <script src="https://cesium.com/downloads/cesiumjs/releases/1.120/Build/Cesium/Cesium.js"></script>
  <link href="https://cesium.com/downloads/cesiumjs/releases/1.120/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
  <style>
    html, body, #cesiumContainer {
      width: 100%;
      height: 100%;
      margin: 0;
      padding: 0;
      overflow: hidden;
      font-family: Arial, sans-serif;
    }
    .panel {
      position: absolute;
      top: 12px;
      left: 12px;
      z-index: 10;
      width: 280px;
      background: rgba(18, 24, 38, 0.86);
      color: #f8fafc;
      border: 1px solid rgba(148, 163, 184, 0.45);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 12px;
      line-height: 1.45;
      box-shadow: 0 8px 28px rgba(15, 23, 42, 0.35);
    }
    .title {
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 6px;
    }
    .legend {
      display: grid;
      grid-template-columns: 14px 1fr;
      gap: 5px 7px;
      margin-top: 8px;
      align-items: center;
    }
    .swatch {
      width: 14px;
      height: 5px;
      border-radius: 999px;
    }
  </style>
</head>
<body>
  <div id="cesiumContainer"></div>
  <div class="panel">
    <div class="title">3D Globe Route Preview</div>
    <div id="summary">Loading route data...</div>
    <div class="legend">
      <span class="swatch" style="background:#2563eb"></span><span>&lt; 8 kn</span>
      <span class="swatch" style="background:#16a34a"></span><span>8 - 12 kn</span>
      <span class="swatch" style="background:#f59e0b"></span><span>12 - 16 kn</span>
      <span class="swatch" style="background:#dc2626"></span><span>&gt;= 16 kn</span>
      <span class="swatch" style="background:#00d4ff"></span><span>no speed value</span>
    </div>
  </div>
  <script>
    const payload = __MAP_PAYLOAD__;
    const viewer = new Cesium.Viewer("cesiumContainer", {
      animation: false,
      baseLayerPicker: false,
      fullscreenButton: true,
      geocoder: false,
      homeButton: true,
      infoBox: true,
      sceneModePicker: true,
      selectionIndicator: true,
      timeline: false,
      navigationHelpButton: true,
      imageryProvider: new Cesium.UrlTemplateImageryProvider({
        url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        maximumLevel: 19,
        credit: "OpenStreetMap"
      })
    });
    viewer.scene.globe.enableLighting = false;
    viewer.scene.screenSpaceCameraController.enableTilt = true;
    viewer.scene.screenSpaceCameraController.enableRotate = true;

    const entities = [];

    function toCartesian(point) {
      return Cesium.Cartesian3.fromDegrees(point.lng, point.lat, point.height || 0);
    }

    function speedColor(speed) {
      if (typeof speed !== "number") return "#00d4ff";
      if (speed < 8) return "#2563eb";
      if (speed < 12) return "#16a34a";
      if (speed < 16) return "#f59e0b";
      return "#dc2626";
    }

    function addEntity(entity) {
      const added = viewer.entities.add(entity);
      entities.push(added);
      return added;
    }

    function addMarker(point, label, color, size) {
      addEntity({
        name: label || "Point",
        position: toCartesian(point),
        point: {
          pixelSize: size || 8,
          color: Cesium.Color.fromCssColorString(color || "#60a5fa"),
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND
        },
        label: {
          text: label || "",
          font: "12px Arial",
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
          pixelOffset: new Cesium.Cartesian2(0, -14),
          showBackground: false,
          scale: 0.75
        }
      });
    }

    function addPolyline(points, color, width, name) {
      if (!points || points.length < 2) return;
      addEntity({
        name: name || "Route",
        polyline: {
          positions: points.map(toCartesian),
          width: width || 3,
          material: Cesium.Color.fromCssColorString(color || "#00d4ff"),
          arcType: Cesium.ArcType.GEODESIC,
          clampToGround: false
        }
      });
    }

    payload.points.forEach((point, index) => {
      addMarker(point, `${index + 1}. ${point.label || "Port"}`, "#38bdf8", 9);
    });

    payload.lines.forEach((line, index) => {
      addPolyline(line, "#00d4ff", 3, `Line ${index + 1}`);
    });

    payload.routes.forEach((route, routeIndex) => {
      const routePoints = route.points || [];
      const hasSpeed = routePoints.some(point => typeof point.speed === "number");
      if (hasSpeed) {
        for (let index = 0; index < routePoints.length - 1; index += 1) {
          const start = routePoints[index];
          const end = routePoints[index + 1];
          const speed = typeof end.speed === "number" ? end.speed : start.speed;
          addPolyline([start, end], speedColor(speed), 4, `${route.label || "Route"} segment ${index + 1}`);
        }
      } else {
        addPolyline(routePoints, "#00d4ff", 4, route.label || `Route ${routeIndex + 1}`);
      }

      const step = Math.max(1, Math.floor(routePoints.length / 250));
      routePoints.forEach((point, index) => {
        const endpoint = index === 0 || index === routePoints.length - 1;
        if (!endpoint && index % step !== 0) return;
        const label = index === 0 ? `R${routeIndex + 1} START` : index === routePoints.length - 1 ? `R${routeIndex + 1} END` : "";
        addMarker(point, label, endpoint ? "#f8fafc" : "#94a3b8", endpoint ? 10 : 4);
      });
    });

    const speedSamples = payload.routes.flatMap(route => (route.points || []).map(point => point.speed)).filter(speed => typeof speed === "number");
    const summary = document.getElementById("summary");
    const pointCount = payload.points.length;
    const lineCount = payload.lines.length;
    const routeCount = payload.routes.length;
    const routeNodeCount = payload.routes.reduce((sum, route) => sum + ((route.points || []).length), 0);
    if (speedSamples.length) {
      const min = Math.min(...speedSamples).toFixed(2);
      const max = Math.max(...speedSamples).toFixed(2);
      const avg = (speedSamples.reduce((sum, value) => sum + value, 0) / speedSamples.length).toFixed(2);
      summary.textContent = `Ports ${pointCount}, lines ${lineCount}, routes ${routeCount}, route nodes ${routeNodeCount}, speed min/avg/max ${min}/${avg}/${max} kn`;
    } else {
      summary.textContent = `Ports ${pointCount}, lines ${lineCount}, routes ${routeCount}, route nodes ${routeNodeCount}, no speed profile values`;
    }

    if (entities.length) {
      viewer.flyTo(entities, { duration: 1.2 });
    } else {
      viewer.camera.flyHome(0);
    }
  </script>
</body>
</html>
"""

    def _render_route_path(self, route_points: List[Dict[str, Any]]) -> None:
        speeds = [point.get("speed") for point in route_points if point.get("speed") is not None]
        if not speeds:
            path = [(point["lat"], point["lng"]) for point in route_points]
            for segment in self._split_antimeridian_path(path):
                self.map_widget.set_path(segment, color="#00d4ff", width=4)
            return

        for start, end in zip(route_points, route_points[1:]):
            speed = end.get("speed") if end.get("speed") is not None else start.get("speed")
            color = self._speed_color(speed)
            path = [(start["lat"], start["lng"]), (end["lat"], end["lng"])]
            for segment in self._split_antimeridian_path(path):
                self.map_widget.set_path(segment, color=color, width=4)

    def _split_antimeridian_path(self, path: List[Tuple[float, float]]) -> List[List[Tuple[float, float]]]:
        if len(path) < 2:
            return []
        segments: List[List[Tuple[float, float]]] = [[path[0]]]
        for start, end in zip(path, path[1:]):
            start_lat, start_lng = start
            end_lat, end_lng = end
            delta = end_lng - start_lng
            if abs(delta) <= 180:
                segments[-1].append(end)
                continue

            if start_lng > 0 > end_lng:
                adjusted_end_lng = end_lng + 360
                fraction = (180 - start_lng) / (adjusted_end_lng - start_lng)
                crossing_lat = start_lat + (end_lat - start_lat) * fraction
                segments[-1].append((crossing_lat, 180))
                segments.append([(crossing_lat, -180), end])
            elif start_lng < 0 < end_lng:
                adjusted_end_lng = end_lng - 360
                fraction = (-180 - start_lng) / (adjusted_end_lng - start_lng)
                crossing_lat = start_lat + (end_lat - start_lat) * fraction
                segments[-1].append((crossing_lat, -180))
                segments.append([(crossing_lat, 180), end])
            else:
                segments[-1].append(end)
        return [segment for segment in segments if len(segment) >= 2]

    def _render_route_nodes(self, route_index: int, route_points: List[Dict[str, Any]]) -> None:
        max_nodes = 200
        step = max(1, len(route_points) // max_nodes)
        last_index = len(route_points) - 1
        for index, point in enumerate(route_points):
            is_endpoint = index in {0, last_index}
            if not is_endpoint and index % step != 0:
                continue
            if index == 0:
                text = f"R{route_index} START"
            elif index == last_index:
                text = f"R{route_index} END"
            elif len(route_points) <= 60:
                text = f"WP {index + 1}"
            else:
                text = "."
            self.map_widget.set_marker(point["lat"], point["lng"], text=text)

    def _speed_color(self, speed: Any) -> str:
        if not isinstance(speed, (int, float)):
            return "#00d4ff"
        if speed < 8:
            return "#2563eb"
        if speed < 12:
            return "#16a34a"
        if speed < 16:
            return "#f59e0b"
        return "#dc2626"

    def _log_speed_profile(self, route_index: int, label: str, route_points: List[Dict[str, Any]]) -> None:
        speeds = [float(point["speed"]) for point in route_points if isinstance(point.get("speed"), (int, float))]
        if not speeds:
            self._log(f"Route {route_index} ({label}) speed profile: no speed values found.")
            return
        average = round(sum(speeds) / len(speeds), 2)
        self._log(
            f"Route {route_index} ({label}) speed profile: min {min(speeds):.2f} kn, "
            f"avg {average:.2f} kn, max {max(speeds):.2f} kn, samples {len(speeds)}"
        )

    def _fit_positions(self, positions: List[Tuple[float, float]]) -> None:
        lats = [lat for lat, _lng in positions]
        lngs = [lng for _lat, lng in positions]
        center_lat = (min(lats) + max(lats)) / 2
        center_lng, west_lng, east_lng, crosses_antimeridian = self._longitude_window(lngs)
        try:
            if crosses_antimeridian:
                self.map_widget.set_position(center_lat, center_lng)
                self.map_widget.set_zoom(self._zoom_for_span(max(lats) - min(lats), east_lng - west_lng))
            else:
                self.map_widget.fit_bounding_box((max(lats), west_lng), (min(lats), east_lng))
        except Exception:
            self.map_widget.set_position(center_lat, center_lng)
            self.map_widget.set_zoom(4 if len(positions) > 1 else 9)

    def _longitude_window(self, lngs: List[float]) -> Tuple[float, float, float, bool]:
        if len(lngs) == 1:
            lng = self._normalize_lng(lngs[0])
            return lng, lng, lng, False

        sorted_lngs = sorted(self._normalize_lng(lng) for lng in lngs)
        gaps = []
        for index, lng in enumerate(sorted_lngs):
            next_lng = sorted_lngs[(index + 1) % len(sorted_lngs)]
            if index == len(sorted_lngs) - 1:
                next_lng += 360
            gaps.append((next_lng - lng, index))
        largest_gap, largest_gap_index = max(gaps)
        west_index = (largest_gap_index + 1) % len(sorted_lngs)
        west = sorted_lngs[west_index]
        east = sorted_lngs[largest_gap_index]
        if east < west:
            east += 360
        center = self._normalize_lng((west + east) / 2)
        return center, west, east, west > 180 or east > 180

    def _normalize_lng(self, lng: float) -> float:
        normalized = ((lng + 180) % 360) - 180
        return 180 if normalized == -180 and lng > 0 else normalized

    def _zoom_for_span(self, lat_span: float, lng_span: float) -> int:
        span = max(lat_span, lng_span)
        if span <= 2:
            return 6
        if span <= 8:
            return 5
        if span <= 25:
            return 4
        if span <= 70:
            return 3
        return 2


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
        self.environment_var = tk.StringVar(value="prod")
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
