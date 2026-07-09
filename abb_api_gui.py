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
import math
import os
import queue
import re
import csv
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Tuple

import requests
import websocket

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None


class ApiResponseError(RuntimeError):
    pass


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
VESSEL_WS_RESPONSE_TIMEOUT_SECONDS = 300
OPTIMAL_BATCH_RETRY_ATTEMPTS = 3
OPTIMAL_BATCH_RETRY_DELAY_SECONDS = 15
PROFILE_SERIES_COLORS = [
    "#38bdf8",
    "#f59e0b",
    "#22c55e",
    "#f472b6",
    "#a78bfa",
    "#ef4444",
    "#14b8a6",
    "#eab308",
]


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


def _use_next_waypoint_speed_rpm(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    original_values: List[Dict[str, float]] = []
    for point in points:
        props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
        values = {}
        for key in ("speed", "rpm"):
            if isinstance(props.get(key), (int, float)):
                values[key] = float(props[key])
        original_values.append(values)

    for index, point in enumerate(points):
        props = point.setdefault("properties", {})
        if not isinstance(props, dict):
            continue
        props.pop("speed", None)
        props.pop("rpm", None)
        if index + 1 < len(original_values):
            props.update(original_values[index + 1])
    return points


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

OPTIMAL_BATCH_VESSEL_PARAMETERS = {
    "imo": "9935208",
    "vesselType": "ContainerVessel",
    "measurements": {
        "beam": 51,
        "lengthOverall": 366,
    },
}

OPTIMAL_BATCH_CONFIG = {
    "hoursBetweenRouteWaypoints": 3,
    "minimumHoursBetweenSpeedChanges": 6,
}

OPTIMAL_BATCH_SPEED_RANGE = {"minimum": 1, "maximum": 50}
BATCH_INSTRUCTED_SPEED = round((OPTIMAL_BATCH_SPEED_RANGE["minimum"] + OPTIMAL_BATCH_SPEED_RANGE["maximum"]) / 2, 1)
BATCH_SPEED_RANGE_ENDPOINTS = {"Recommended set speed", "Fixed ETA", "Optimal set speed"}
BATCH_OUTPUT_KINDS = {
    "Shortest path": "ShortestPath",
    "Instructed set speed": "InstructedSpeed",
    "Recommended set speed": "RecommendedSpeed",
    "Fixed ETA": "FixedETA",
    "Optimal set speed": "Optimal",
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
        "optimizationType": "Fuel",
    },
    "Recommended set speed": {
        "id": "recommended-speed-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
        "config": VESSEL_SAMPLE_CONFIG,
        "speeds": [{"minimum": 8, "maximum": 12}],
        "optimizationType": "Fuel",
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
        "optimizationType": "Fuel",
    },
    "Optimal set speed": {
        "id": "optimal-speed-1",
        "points": DEFAULT_SHORTEST_PATH_REQUEST["points"],
        "voyage": VESSEL_SAMPLE_VOYAGE,
        "etd": "2026-07-06T19:20:30Z",
        "vesselParameters": VESSEL_SAMPLE_PARAMETERS,
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

DEFAULT_CONTINUOUS_BATCH_ROOT = Path(r"C:\Users\Device_SHI\Downloads\EVERMAX_Yantian2Panama\EVERMAX_Yantian2Panama")
DEFAULT_CONTINUOUS_PLANNED_RTZ = DEFAULT_CONTINUOUS_BATCH_ROOT / "9935208_SAS_Planned_20260606_092117.rtz"
DEFAULT_CONTINUOUS_OPTIMAL_DIR = DEFAULT_CONTINUOUS_BATCH_ROOT / "optimal"
DEFAULT_CONTINUOUS_OUTPUT_DIR = DEFAULT_CONTINUOUS_BATCH_ROOT / "abb_optimal"
RTZ_FILE_NAME_RE = re.compile(
    r"^(?P<imo>\d+)_SAS_(?P<kind>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.rtz$",
    re.IGNORECASE,
)

EARTH_TEXTURE_URL = (
    "https://assets.science.nasa.gov/content/dam/science/esd/eo/images/bmng/"
    "bmng-base/january/world.200401.3x5400x2700.jpg"
)
EARTH_TEXTURE_CACHE_NAME = "blue_marble_200401_2048.jpg"
EARTH_TEXTURE_WIDTH = 2048
EARTH_TEXTURE_HEIGHT = 1024

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
        token = body.get("access_token")
        if not token:
            raise ValueError("Token response did not include access_token.")
        self.access_token = str(token)
        expires_in = int(body.get("expires_in", 0) or 0)
        expires_at = time.time() + expires_in if expires_in > 0 else 0
        self.access_token_expires_at = expires_at
        root = self.winfo_toplevel()
        if hasattr(root, "shared_auth"):
            root.shared_auth.update(
                {
                    "client_id": data.get("client_id", ""),
                    "client_secret": data.get("client_secret", ""),
                    "scope": data.get("scope", ""),
                    "token_url": self.token_url_var.get().strip(),
                    "access_token": self.access_token,
                    "expires_at": expires_at,
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
        if shared.get("expires_at"):
            self.access_token_expires_at = shared["expires_at"]
        self.log("Shared auth copied into this tab.")

    def _effective_token(self) -> Optional[str]:
        refresh_margin_seconds = 60
        minimum_valid_until = time.time() + refresh_margin_seconds
        local_expires_at = float(getattr(self, "access_token_expires_at", 0) or 0)
        if self.access_token and (not local_expires_at or local_expires_at > minimum_valid_until):
            return self.access_token
        root = self.winfo_toplevel()
        shared = getattr(root, "shared_auth", {})
        shared_token = shared.get("access_token")
        shared_expires_at = float(shared.get("expires_at", 0) or 0)
        if shared_token and (not shared_expires_at or shared_expires_at > minimum_valid_until):
            self.access_token = shared_token
            self.access_token_expires_at = shared_expires_at
            return shared_token
        return None

    def _require_token(self) -> str:
        token = self._effective_token()
        if token:
            return token
        return self._request_token_sync("No valid access token in memory. Requesting token automatically...")

    def _auth_data(self) -> Dict[str, str]:
        data = {
            "client_id": self.client_id_var.get().strip(),
            "client_secret": self.client_secret_var.get().strip(),
            "scope": self.scope_var.get().strip(),
            "grant_type": "client_credentials",
        }
        if not data["client_id"] or not data["client_secret"]:
            raise ValueError("Client ID and Client Secret are required.")
        return data

    def _request_token_sync(self, reason: str = "Requesting token...") -> str:
        data = self._auth_data()
        self.log(reason)
        resp = requests.post(self.token_url_var.get().strip(), data=data, headers=self._token_headers(), timeout=30)
        self.log(f"Token response HTTP {resp.status_code}")
        self._log_rate_limit_headers(resp)
        self._raise_for_token_response(resp)
        body = resp.json()
        self._remember_auth(data, body)
        self.log(json.dumps({k: v for k, v in body.items() if k != "access_token"}, indent=2))
        self.log("Token saved in memory.")
        return self.access_token or ""

    def _request_token_async(self) -> None:
        def worker() -> None:
            try:
                self._request_token_sync()
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Token Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}\n")

    def _flush_log_queue(self) -> None:
        while not self.log_queue.empty():
            self.log_text.insert(tk.END, self.log_queue.get())
            self.log_text.see(tk.END)
        self.after(100, self._flush_log_queue)

    def _log_response(self, resp: requests.Response) -> None:
        parsed: Any = None
        try:
            parsed = resp.json()
            self.latest_response_data = parsed
            text = json.dumps(parsed, indent=2)
        except Exception:
            text = resp.text
        self.log(text[:50000])
        if parsed is not None:
            self._log_api_errors(parsed)
        if not resp.ok:
            self.log(f"HTTP error response status: {resp.status_code}")

    def _api_error_messages(self, data: Any) -> List[str]:
        messages: List[str] = []
        seen = set()

        def add(message: str) -> None:
            text = " ".join(str(message).split())
            if text and text not in seen:
                seen.add(text)
                messages.append(text)

        def format_error(obj: Dict[str, Any]) -> str:
            parts = []
            for key in ("status", "title", "error", "errorDescription", "detail", "message"):
                value = obj.get(key)
                if value not in (None, ""):
                    parts.append(f"{key}={value}")
            return "; ".join(parts) or json.dumps(obj, ensure_ascii=False)[:1000]

        def visit(obj: Any) -> None:
            if isinstance(obj, dict):
                errors = obj.get("errors")
                if isinstance(errors, list):
                    for item in errors:
                        if isinstance(item, dict):
                            add(format_error(item))
                        else:
                            add(str(item))
                elif isinstance(errors, dict):
                    add(format_error(errors))
                status_value = obj.get("status")
                if str(status_value).lower() == "failed":
                    add(format_error(obj))
                if "error" in obj or "errorDescription" in obj:
                    add(format_error(obj))
                for key, value in obj.items():
                    if key != "errors":
                        visit(value)
            elif isinstance(obj, list):
                for item in obj:
                    visit(item)

        visit(data)
        return messages

    def _log_api_errors(self, data: Any, context: str = "API response") -> List[str]:
        messages = self._api_error_messages(data)
        for index, message in enumerate(messages[:20], start=1):
            self.log(f"{context} error {index}: {message}")
        if len(messages) > 20:
            self.log(f"{context} has {len(messages) - 20} more errors.")
        return messages

    def _raise_for_api_errors(self, data: Any, context: str = "API response") -> None:
        messages = self._log_api_errors(data, context)
        if messages:
            raise ApiResponseError(f"{context} contained errors: {messages[0]}")

    def _raise_for_http_response(self, resp: requests.Response, context: str = "HTTP response") -> None:
        if resp.ok:
            return
        content_type = resp.headers.get("Content-Type", "")
        self.log(f"{context} error Content-Type: {content_type or 'unknown'}")
        try:
            parsed = resp.json()
            self.log(json.dumps(parsed, indent=2)[:20000])
            self._log_api_errors(parsed, context)
        except Exception:
            body = resp.text.strip()
            self.log(body[:20000] if body else "<empty response body>")
        resp.raise_for_status()

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
        self.batch_stop_event = threading.Event()
        self.batch_running = False
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

        batch_frame = ttk.LabelFrame(left, text="Continuous RTZ Batch")
        batch_frame.pack(fill=tk.X, pady=(0, 8))
        self.batch_planned_path_var = tk.StringVar(value=str(DEFAULT_CONTINUOUS_PLANNED_RTZ))
        self.batch_optimal_dir_var = tk.StringVar(value=str(DEFAULT_CONTINUOUS_OPTIMAL_DIR))
        self.batch_output_dir_var = tk.StringVar(value=str(DEFAULT_CONTINUOUS_OUTPUT_DIR))
        self.batch_endpoint_var = tk.StringVar(value="Optimal set speed")
        self.batch_limit_var = tk.StringVar(value="0")
        ttk.Label(batch_frame, text="Planned RTZ").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_planned_path_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_planned_file).grid(row=0, column=2, padx=5, pady=3)
        ttk.Label(batch_frame, text="Reference optimal folder").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_optimal_dir_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_optimal_dir).grid(row=1, column=2, padx=5, pady=3)
        ttk.Label(batch_frame, text="Output folder").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_output_dir_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_output_dir).grid(row=2, column=2, padx=5, pady=3)
        ttk.Label(batch_frame, text="Batch request").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(
            batch_frame,
            textvariable=self.batch_endpoint_var,
            values=list(VESSEL_ASYNC_ENDPOINTS.keys()),
            state="readonly",
            width=28,
        ).grid(row=3, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(batch_frame, text="Max files (0 = all)").grid(row=4, column=0, sticky="w", padx=5, pady=3)
        ttk.Spinbox(batch_frame, textvariable=self.batch_limit_var, from_=0, to=10000, increment=1, width=10).grid(row=4, column=1, sticky="w", padx=5, pady=3)
        ttk.Button(batch_frame, text="Start Batch", command=self.start_continuous_optimal_batch).grid(row=4, column=1, sticky="e", padx=5, pady=3)
        ttk.Button(batch_frame, text="Stop", command=self.stop_continuous_optimal_batch).grid(row=4, column=2, padx=5, pady=3)
        batch_frame.columnconfigure(1, weight=1)

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

    def browse_batch_planned_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select planned RTZ",
            initialdir=str(DEFAULT_CONTINUOUS_BATCH_ROOT),
            filetypes=[("RTZ files", "*.rtz"), ("XML files", "*.xml"), ("All files", "*.*")],
        )
        if path:
            self.batch_planned_path_var.set(path)

    def browse_batch_optimal_dir(self) -> None:
        path = filedialog.askdirectory(title="Select optimal RTZ folder", initialdir=str(DEFAULT_CONTINUOUS_BATCH_ROOT))
        if path:
            self.batch_optimal_dir_var.set(path)

    def browse_batch_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Select ABB output RTZ folder", initialdir=str(DEFAULT_CONTINUOUS_BATCH_ROOT))
        if path:
            self.batch_output_dir_var.set(path)

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
        schedule_paths = (
            [".//rtz:scheduleElement", ".//rtz:sheduleElement"] if namespace else [".//scheduleElement", ".//sheduleElement"]
        )
        vo_path = ".//rtz:VOElement" if namespace else ".//VOElement"

        schedule_by_waypoint: Dict[str, Dict[str, Any]] = {}
        speeds = []
        etd = None
        eta = None
        for schedule_path in schedule_paths:
            for item in root.findall(schedule_path, namespace):
                waypoint_id = item.attrib.get("waypointId")
                waypoint_schedule: Dict[str, Any] = {}
                if item.attrib.get("speed"):
                    speed = float(item.attrib["speed"])
                    speeds.append(speed)
                    waypoint_schedule["speed"] = speed
                if item.attrib.get("rpm"):
                    waypoint_schedule["rpm"] = float(item.attrib["rpm"])
                if item.attrib.get("etd"):
                    waypoint_schedule["etd"] = item.attrib["etd"]
                if item.attrib.get("eta"):
                    waypoint_schedule["eta"] = item.attrib["eta"]
                if waypoint_id:
                    schedule_by_waypoint[waypoint_id] = waypoint_schedule
                etd = etd or item.attrib.get("etd")
                eta = item.attrib.get("eta") or eta

        for item in root.findall(vo_path, namespace):
            waypoint_id = item.attrib.get("waypointId")
            if waypoint_id:
                waypoint_schedule = schedule_by_waypoint.setdefault(waypoint_id, {})
                if item.attrib.get("speed") and "speed" not in waypoint_schedule:
                    speed = float(item.attrib["speed"])
                    waypoint_schedule["speed"] = speed
                    speeds.append(speed)
                if item.attrib.get("rpm") and "rpm" not in waypoint_schedule:
                    waypoint_schedule["rpm"] = float(item.attrib["rpm"])

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

        _use_next_waypoint_speed_rpm(points)
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

    def start_continuous_optimal_batch(self) -> None:
        if self.batch_running:
            messagebox.showinfo("Continuous RTZ Batch", "A batch is already running.")
            return
        self.batch_stop_event.clear()
        threading.Thread(target=self._run_continuous_optimal_batch, daemon=True).start()

    def stop_continuous_optimal_batch(self) -> None:
        self.batch_stop_event.set()
        self.log("Continuous RTZ batch stop requested.")

    def _run_continuous_optimal_batch(self) -> None:
        self.batch_running = True
        try:
            endpoint_name = self.batch_endpoint_var.get().strip()
            if endpoint_name not in VESSEL_ASYNC_ENDPOINTS:
                raise ValueError(f"Unsupported batch request type: {endpoint_name}")
            planned_path = Path(self.batch_planned_path_var.get().strip())
            optimal_dir = Path(self.batch_optimal_dir_var.get().strip())
            output_dir = Path(self.batch_output_dir_var.get().strip())
            if not planned_path.exists():
                raise ValueError(f"Planned RTZ not found: {planned_path}")
            if not optimal_dir.exists():
                raise ValueError(f"Optimal RTZ folder not found: {optimal_dir}")
            output_dir.mkdir(parents=True, exist_ok=True)

            planned_points, planned_schedule = self._parse_rtz(planned_path)
            if len(planned_points) < 2:
                raise ValueError("Planned RTZ must contain at least two waypoints.")

            optimal_files = self._optimal_rtz_files(optimal_dir)
            rpm_sog_files = list(optimal_files)
            limit = self._batch_limit()
            if limit:
                optimal_files = optimal_files[:limit]

            rpm_sog_profile = self._build_rpm_sog_profile(rpm_sog_files)
            if rpm_sog_profile:
                self.log(
                    "RPM-SOG profile confirmed: "
                    f"{rpm_sog_profile['pairs']} samples, "
                    f"{len(rpm_sog_profile['curve'])} speed bins, "
                    f"SOG {rpm_sog_profile['speed_min']:.2f}-{rpm_sog_profile['speed_max']:.2f} kn, "
                    f"RPM {rpm_sog_profile['rpm_min']:.2f}-{rpm_sog_profile['rpm_max']:.2f}."
                )
            else:
                self.log("RPM-SOG profile not available. Output RTZ files will keep ABB speed values only.")

            entries = []
            for sequence, optimal_path in enumerate(optimal_files, start=1):
                optimal_points, optimal_schedule = self._parse_rtz(optimal_path)
                if not optimal_points:
                    raise ValueError(f"Optimal RTZ has no waypoint: {optimal_path}")
                remaining_points = self._remaining_planned_points(planned_points, optimal_points[0])
                if len(remaining_points) < 2:
                    raise ValueError(f"Could not build at least two waypoints for: {optimal_path}")
                schedule = dict(optimal_schedule)
                metadata = self._rtz_file_metadata(optimal_path)
                if metadata.get("timestamp_iso"):
                    schedule["etd"] = metadata["timestamp_iso"]
                    schedule["weatherSource"] = {"type": "Forecast", "version": metadata["timestamp_iso"]}
                entries.append({"source": optimal_path, "points": remaining_points, "schedule": schedule, "sequence": sequence})

            self.log(
                f"Continuous RTZ batch prepared: {len(entries)} {endpoint_name} requests "
                f"for {len(optimal_files)} reference optimal files."
            )
            for index, entry in enumerate(entries, start=1):
                if self.batch_stop_event.is_set():
                    self.log("Continuous RTZ batch stopped.")
                    break

                source_path = entry["source"]
                metadata = self._rtz_file_metadata(source_path)
                output_name = self._batch_output_name(source_path, endpoint_name)
                output_path = output_dir / output_name
                request_path = output_path.with_suffix(".request.json")
                websocket_path = output_path.with_suffix(".websocket.json")
                response_path = output_path.with_suffix(".response.json")
                if self._resume_batch_output_if_possible(output_path, websocket_path, response_path, rpm_sog_profile):
                    continue

                payload = self._build_rtz_request(endpoint_name, entry["points"], entry["schedule"])
                payload["id"] = f"abb-{self._batch_endpoint_slug(endpoint_name)}-{metadata.get('timestamp_label') or index}"
                if metadata.get("timestamp_iso"):
                    payload["etd"] = metadata["timestamp_iso"]
                    if endpoint_name == "Fixed ETA":
                        etd = _parse_utc(metadata["timestamp_iso"])
                        if etd:
                            payload["eta"] = _utc_z(etd + timedelta(days=FIXED_ETA_DURATION_DAYS))
                if entry["schedule"].get("weatherSource"):
                    payload["weatherSource"] = entry["schedule"]["weatherSource"]
                payload = self._minimal_batch_payload(payload, endpoint_name)

                self._write_batch_json(request_path, payload)
                self._run_optimal_batch_request_with_retries(
                    endpoint_name,
                    index,
                    len(entries),
                    source_path,
                    output_path,
                    output_name,
                    payload,
                    websocket_path,
                    response_path,
                    rpm_sog_profile,
                )
            self.log("Continuous RTZ batch finished.")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            messagebox.showerror("Continuous RTZ Batch Error", str(exc))
        finally:
            self.batch_running = False

    def _batch_limit(self) -> int:
        try:
            value = int(self.batch_limit_var.get())
        except ValueError:
            return 0
        return max(0, value)

    def _optimal_rtz_files(self, optimal_dir: Path) -> List[Path]:
        return sorted(optimal_dir.glob("*.rtz"), key=lambda path: (self._rtz_file_metadata(path).get("timestamp_sort") or path.name, path.name))

    def _rtz_file_metadata(self, path: Path) -> Dict[str, Any]:
        match = RTZ_FILE_NAME_RE.match(path.name)
        if not match:
            return {"kind": "", "timestamp_label": "", "timestamp_iso": "", "timestamp_sort": path.name}
        label = f"{match.group('date')}_{match.group('time')}"
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "imo": match.group("imo"),
            "kind": match.group("kind"),
            "timestamp_label": label,
            "timestamp_iso": _utc_z(dt),
            "timestamp_sort": dt,
        }

    def _batch_output_name(self, source_path: Path, endpoint_name: str) -> str:
        metadata = self._rtz_file_metadata(source_path)
        output_kind = BATCH_OUTPUT_KINDS.get(endpoint_name, self._batch_endpoint_slug(endpoint_name))
        if metadata.get("imo") and metadata.get("timestamp_label"):
            return f"{metadata['imo']}_SAS_{output_kind}_{metadata['timestamp_label']}.rtz"
        return f"{source_path.stem}_{output_kind}.rtz"

    def _batch_endpoint_slug(self, endpoint_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", endpoint_name.lower()).strip("-") or "batch"

    def _write_batch_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_batch_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _run_optimal_batch_request_with_retries(
        self,
        endpoint_name: str,
        index: int,
        total: int,
        source_path: Path,
        output_path: Path,
        output_name: str,
        payload: Dict[str, Any],
        websocket_path: Path,
        response_path: Path,
        rpm_sog_profile: Optional[Dict[str, Any]],
    ) -> None:
        for attempt in range(1, OPTIMAL_BATCH_RETRY_ATTEMPTS + 1):
            if self.batch_stop_event.is_set():
                self.log("Continuous RTZ batch stopped.")
                return
            try:
                if self._resume_batch_output_if_possible(output_path, websocket_path, response_path, rpm_sog_profile):
                    return
                retry_text = "" if attempt == 1 else f" (retry {attempt}/{OPTIMAL_BATCH_RETRY_ATTEMPTS})"
                self.log(f"[{index}/{total}] Requesting {endpoint_name} from {source_path.name} -> {output_name}{retry_text}")
                response_data = self._send_ws_request_sync(endpoint_name, payload)
                self._write_batch_json(websocket_path, response_data)
                self._raise_for_api_errors(response_data, f"{endpoint_name} WebSocket response")
                route_data = self._download_route_response(response_data) or response_data
                self._write_batch_json(response_path, route_data)
                self._raise_for_api_errors(route_data, f"{endpoint_name} route response")
                self.log(f"Saved JSON: {output_path.with_suffix('.request.json').name}, {websocket_path.name}, {response_path.name}")
                if self._save_optimal_batch_route(output_path, route_data, rpm_sog_profile):
                    return
                return
            except ApiResponseError:
                self.batch_stop_event.set()
                self.log("Continuous RTZ batch stopped because the API response contained errors.")
                raise
            except Exception as exc:
                if not self._is_connection_reset_10054(exc) or attempt >= OPTIMAL_BATCH_RETRY_ATTEMPTS:
                    raise
                self.log(
                    f"Connection reset 10054 during batch request. "
                    f"Restarting current item after {OPTIMAL_BATCH_RETRY_DELAY_SECONDS}s "
                    f"({attempt}/{OPTIMAL_BATCH_RETRY_ATTEMPTS})."
                )
                if self.batch_stop_event.wait(OPTIMAL_BATCH_RETRY_DELAY_SECONDS):
                    self.log("Continuous RTZ batch stopped.")
                    return

    def _save_optimal_batch_route(
        self,
        output_path: Path,
        route_data: Any,
        rpm_sog_profile: Optional[Dict[str, Any]],
    ) -> bool:
        route_points = self._extract_route_points_for_rtz(route_data)
        if len(route_points) < 2:
            self.log(f"No route geometry found. Response saved: {output_path.with_suffix('.response.json')}")
            return False
        if rpm_sog_profile:
            rpm_count = self._apply_rpm_sog_profile(route_points, rpm_sog_profile)
            self.log(f"Applied RPM from SOG profile: {rpm_count}/{len(route_points)} route waypoints.")
        self._write_rtz(output_path, route_points, route_name=output_path.stem)
        self.log(f"Saved RTZ: {output_path} ({len(route_points)} waypoints)")
        return True

    def _is_connection_reset_10054(self, exc: BaseException) -> bool:
        if isinstance(exc, ConnectionResetError):
            return True
        if getattr(exc, "errno", None) == 10054 or getattr(exc, "winerror", None) == 10054:
            return True
        text = str(exc)
        return "10054" in text or "connection reset" in text.lower()

    def _minimal_batch_payload(self, payload: Dict[str, Any], endpoint_name: str) -> Dict[str, Any]:
        minimal: Dict[str, Any] = {}
        if endpoint_name == "Shortest path" and "type" in payload:
            minimal["type"] = json.loads(json.dumps(payload["type"]))
        if "id" in payload:
            minimal["id"] = json.loads(json.dumps(payload["id"]))
        if "points" in payload:
            minimal["points"] = self._sanitize_batch_points(payload["points"])
        if endpoint_name == "Shortest path":
            return minimal
        if "voyage" in payload:
            voyage = json.loads(json.dumps(payload["voyage"]))
            if isinstance(voyage, dict) and isinstance(voyage.get("ports"), list):
                voyage["ports"] = self._sanitize_batch_points(voyage["ports"])
            minimal["voyage"] = voyage
        for key in ("etd", "weatherSource"):
            if key in payload:
                minimal[key] = json.loads(json.dumps(payload[key]))
        if endpoint_name == "Fixed ETA" and "eta" in payload:
            minimal["eta"] = json.loads(json.dumps(payload["eta"]))
        if endpoint_name == "Instructed set speed":
            minimal["speed"] = BATCH_INSTRUCTED_SPEED
        elif endpoint_name in BATCH_SPEED_RANGE_ENDPOINTS:
            minimal["speeds"] = [dict(OPTIMAL_BATCH_SPEED_RANGE)]
        minimal["vesselParameters"] = json.loads(json.dumps(OPTIMAL_BATCH_VESSEL_PARAMETERS))
        minimal["optimizationType"] = "Fuel"
        minimal["config"] = dict(OPTIMAL_BATCH_CONFIG)
        return minimal

    def _sanitize_batch_points(self, points: Any) -> Any:
        if not isinstance(points, list):
            return json.loads(json.dumps(points))
        sanitized = []
        allowed_property_keys = ("name", "port")
        for index, point in enumerate(points):
            feature = json.loads(json.dumps(point))
            if isinstance(feature, dict):
                for key in ("speed", "rpm", "etd", "eta", "time", "timestamp", "forceRhumbLine", "legBehaviour"):
                    feature.pop(key, None)
                props = feature.get("properties")
                sanitized_props: Dict[str, Any] = {}
                if isinstance(props, dict):
                    sanitized_props = {key: props[key] for key in allowed_property_keys if key in props and props[key] is not None}
                if index > 0:
                    sanitized_props["legBehaviour"] = "ForceSingleRhumbLine"
                feature["properties"] = sanitized_props
            sanitized.append(feature)
        return sanitized

    def _resume_batch_output_if_possible(
        self,
        output_path: Path,
        websocket_path: Path,
        response_path: Path,
        rpm_sog_profile: Optional[Dict[str, Any]],
    ) -> bool:
        if output_path.exists() and output_path.stat().st_size > 0:
            self.log(f"Resume: existing RTZ found. Skipping API request: {output_path.name}")
            return True

        route_data = None
        source_path = None
        if response_path.exists() and response_path.stat().st_size > 0:
            try:
                route_data = self._read_batch_json(response_path)
                source_path = response_path
            except Exception as exc:
                self.log(f"Resume: existing response JSON could not be read. Request will be retried: {response_path.name} ({exc})")
        elif websocket_path.exists() and websocket_path.stat().st_size > 0:
            try:
                websocket_data = self._read_batch_json(websocket_path)
                route_data = self._download_route_response(websocket_data) or websocket_data
                self._write_batch_json(response_path, route_data)
                source_path = response_path
                self.log(f"Resume: response JSON recovered from existing WebSocket JSON: {response_path.name}")
            except Exception as exc:
                self.log(f"Resume: existing WebSocket JSON could not be reused. Request will be retried: {websocket_path.name} ({exc})")

        if route_data is None:
            return False

        self._raise_for_api_errors(route_data, "Existing batch response")

        route_points = self._extract_route_points_for_rtz(route_data)
        if len(route_points) < 2:
            source_name = source_path.name if source_path else response_path.name
            self.log(f"Resume: existing JSON has no route geometry. Request will be retried: {source_name}")
            return False

        if rpm_sog_profile:
            rpm_count = self._apply_rpm_sog_profile(route_points, rpm_sog_profile)
            self.log(f"Resume: applied RPM from SOG profile: {rpm_count}/{len(route_points)} route waypoints.")
        self._write_rtz(output_path, route_points, route_name=output_path.stem)
        self.log(f"Resume: rebuilt RTZ from existing JSON: {output_path} ({len(route_points)} waypoints)")
        return True

    def _build_rpm_sog_profile(self, optimal_files: List[Path]) -> Optional[Dict[str, Any]]:
        pairs: List[Tuple[float, float]] = []
        for optimal_path in optimal_files:
            try:
                points, _schedule = self._parse_rtz(optimal_path)
            except Exception as exc:
                self.log(f"RPM-SOG profile skipped unreadable RTZ: {optimal_path.name} ({exc})")
                continue
            for point in points:
                props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
                speed = props.get("speed")
                rpm = props.get("rpm")
                if isinstance(speed, (int, float)) and isinstance(rpm, (int, float)) and speed > 0 and rpm > 0:
                    pairs.append((float(speed), float(rpm)))

        if len(pairs) < 2:
            return None

        rpm_by_speed: Dict[float, List[float]] = {}
        for speed, rpm in pairs:
            rpm_by_speed.setdefault(round(speed, 2), []).append(rpm)

        curve = sorted((speed, sum(values) / len(values)) for speed, values in rpm_by_speed.items())
        if not curve:
            return None

        speeds = [speed for speed, _rpm in pairs]
        rpms = [rpm for _speed, rpm in pairs]
        return {
            "pairs": len(pairs),
            "curve": curve,
            "speed_min": min(speeds),
            "speed_max": max(speeds),
            "rpm_min": min(rpms),
            "rpm_max": max(rpms),
        }

    def _apply_rpm_sog_profile(self, route_points: List[Dict[str, Any]], profile: Dict[str, Any]) -> int:
        applied = 0
        for point in route_points:
            props = point.setdefault("properties", {})
            if not isinstance(props, dict):
                continue
            if isinstance(props.get("rpm"), (int, float)):
                applied += 1
                continue
            speed = props.get("speed")
            if not isinstance(speed, (int, float)):
                continue
            rpm = self._rpm_from_sog(float(speed), profile)
            if rpm is None:
                continue
            props["rpm"] = round(rpm, 2)
            applied += 1
        return applied

    def _rpm_from_sog(self, speed: float, profile: Dict[str, Any]) -> Optional[float]:
        curve = profile.get("curve")
        if not isinstance(curve, list) or not curve:
            return None
        if len(curve) == 1:
            return float(curve[0][1])
        if speed <= curve[0][0]:
            return float(curve[0][1])
        if speed >= curve[-1][0]:
            return float(curve[-1][1])

        for index in range(1, len(curve)):
            lower_speed, lower_rpm = curve[index - 1]
            upper_speed, upper_rpm = curve[index]
            if speed <= upper_speed:
                if upper_speed == lower_speed:
                    return float(upper_rpm)
                ratio = (speed - lower_speed) / (upper_speed - lower_speed)
                return float(lower_rpm + (upper_rpm - lower_rpm) * ratio)
        return float(curve[-1][1])

    def _remaining_planned_points(self, planned_points: List[Dict[str, Any]], current_point: Dict[str, Any]) -> List[Dict[str, Any]]:
        if len(planned_points) < 2:
            return planned_points
        current_lat, current_lng = self._feature_lat_lng(current_point)
        best_index = 0
        best_score = float("inf")
        for index in range(len(planned_points) - 1):
            start_lat, start_lng = self._feature_lat_lng(planned_points[index])
            end_lat, end_lng = self._feature_lat_lng(planned_points[index + 1])
            score = self._point_segment_score(current_lat, current_lng, start_lat, start_lng, end_lat, end_lng)
            if score < best_score:
                best_score = score
                best_index = index
        cut_index = min(best_index + 1, len(planned_points) - 1)
        if self._distance_nm(current_point, planned_points[cut_index]) < 0.2 and cut_index + 1 < len(planned_points):
            cut_index += 1
        current_feature = json.loads(json.dumps(current_point))
        current_feature.setdefault("properties", {})
        if isinstance(current_feature["properties"], dict):
            current_feature["properties"]["name"] = current_feature["properties"].get("name") or "Current position"
        return [current_feature] + json.loads(json.dumps(planned_points[cut_index:]))

    def _feature_lat_lng(self, feature: Dict[str, Any]) -> Tuple[float, float]:
        coordinates = feature["geometry"]["coordinates"]
        return float(coordinates[1]), float(coordinates[0])

    def _point_segment_score(
        self,
        point_lat: float,
        point_lng: float,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
    ) -> float:
        scale = math.cos(math.radians((point_lat + start_lat + end_lat) / 3))
        end_lng = start_lng + ((end_lng - start_lng + 180) % 360) - 180
        point_lng = start_lng + ((point_lng - start_lng + 180) % 360) - 180
        px, py = point_lng * scale, point_lat
        sx, sy = start_lng * scale, start_lat
        ex, ey = end_lng * scale, end_lat
        dx, dy = ex - sx, ey - sy
        if dx == 0 and dy == 0:
            return (px - sx) ** 2 + (py - sy) ** 2
        fraction = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)))
        closest_x = sx + fraction * dx
        closest_y = sy + fraction * dy
        return (px - closest_x) ** 2 + (py - closest_y) ** 2

    def _distance_nm(self, first: Dict[str, Any], second: Dict[str, Any]) -> float:
        lat1, lng1 = self._feature_lat_lng(first)
        lat2, lng2 = self._feature_lat_lng(second)
        radius_nm = 3440.065
        d_lat = math.radians(lat2 - lat1)
        d_lng = math.radians(((lng2 - lng1 + 180) % 360) - 180)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
        return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))

    def _send_ws_request_sync(self, endpoint_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path, _scope = VESSEL_ASYNC_ENDPOINTS[endpoint_name]
        url = self.ws_base_var.get().rstrip("/") + path
        headers = [
            f"Authorization: Bearer {self._require_token()}",
            f"client_id: {self.ws_client_id_var.get().strip()}",
        ]
        ws = websocket.create_connection(url, header=headers, timeout=30)
        ws.settimeout(VESSEL_WS_RESPONSE_TIMEOUT_SECONDS)
        messages = []
        try:
            ws.send(json.dumps(payload))
            while True:
                parsed = json.loads(ws.recv())
                messages.append(parsed)
                status = str(parsed.get("status", "")).lower()
                if status in {"finished", "failed", "success", "completed"}:
                    break
                if any(key in parsed for key in ("url", "downloadUrl", "responseUrl")):
                    break
        finally:
            ws.close()
        latest = messages[-1] if messages else {}
        return {"webSocketMessages": messages, "latestMessage": latest}

    def _download_route_response(self, response_data: Dict[str, Any]) -> Optional[Any]:
        urls = self._find_download_urls(response_data)
        if not urls:
            return None
        url = urls[0]
        self.log(f"Downloading route response: {url}")
        resp = requests.get(url, timeout=120)
        self.log(f"Route download HTTP {resp.status_code}")
        self._raise_for_http_response(resp, "Route download")
        parsed = resp.json()
        self._raise_for_api_errors(parsed, "Route download response")
        return parsed

    def _find_download_urls(self, data: Any) -> List[str]:
        urls: List[str] = []
        seen = set()

        def visit(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in {"downloadUrl", "responseUrl", "url"} and isinstance(value, str) and value.startswith(("http://", "https://")):
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

    def _extract_route_points_for_rtz(self, data: Any) -> List[Dict[str, Any]]:
        candidates: List[List[Dict[str, Any]]] = []

        def coord_pair(value: Any) -> Optional[Tuple[float, float]]:
            if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
                lon, lat = float(value[0]), float(value[1])
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    return lat, lon
            return None

        def numeric_value(obj: Any, keys: Tuple[str, ...]) -> Optional[float]:
            if not isinstance(obj, dict):
                return None
            for key in keys:
                value = obj.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            props = obj.get("properties")
            return numeric_value(props, keys) if isinstance(props, dict) else None

        def feature_from_point(obj: Any, index: int) -> Optional[Dict[str, Any]]:
            if not isinstance(obj, dict):
                return None
            geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
            pair = coord_pair(geometry.get("coordinates"))
            if pair is None:
                return None
            lat, lon = pair
            props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
            properties = {"name": str(props.get("name") or obj.get("name") or f"WP {index}")}
            speed = numeric_value(obj, ("speed", "speedOverGround", "speedKnots", "sog", "plannedSpeed"))
            if speed is not None:
                properties["speed"] = speed
            rpm = numeric_value(obj, ("rpm", "engineRpm", "shaftRpm"))
            if rpm is not None:
                properties["rpm"] = rpm
            for key in ("eta", "etd", "time", "timestamp"):
                if props.get(key):
                    properties[key] = props[key]
            return {"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [lon, lat]}}

        def visit(obj: Any) -> None:
            if isinstance(obj, dict):
                geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
                coords = geometry.get("coordinates")
                if geometry.get("type") == "LineString" and isinstance(coords, list):
                    line_points = []
                    for index, item in enumerate(coords, start=1):
                        pair = coord_pair(item)
                        if pair:
                            lat, lon = pair
                            line_points.append({"type": "Feature", "properties": {"name": f"WP {index}"}, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
                    if len(line_points) >= 2:
                        candidates.append(line_points)
                points = obj.get("points")
                if isinstance(points, list):
                    route_points = [point for index, item in enumerate(points, start=1) if (point := feature_from_point(item, index))]
                    if len(route_points) >= 2:
                        candidates.append(route_points)
                for value in obj.values():
                    visit(value)
            elif isinstance(obj, list):
                for item in obj:
                    visit(item)

        visit(data)
        return max(candidates, key=len) if candidates else []

    def _write_rtz(self, output_path: Path, route_points: List[Dict[str, Any]], route_name: str) -> None:
        namespace = "http://www.cirm.org/RTZ/1/0"
        ET.register_namespace("", namespace)
        route = ET.Element(f"{{{namespace}}}route", {"version": "1.0"})
        ET.SubElement(route, f"{{{namespace}}}routeInfo", {"routeName": route_name})
        waypoints = ET.SubElement(route, f"{{{namespace}}}waypoints")
        has_schedule = False
        speeds_by_index: Dict[int, float] = {}
        rpms_by_index: Dict[int, float] = {}
        times_by_index: Dict[int, Dict[str, str]] = {}

        for index, point in enumerate(route_points):
            lat, lon = self._feature_lat_lng(point)
            props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
            waypoint = ET.SubElement(waypoints, f"{{{namespace}}}waypoint", {"id": str(index), "name": str(props.get("name") or ""), "radius": "0.50"})
            ET.SubElement(waypoint, f"{{{namespace}}}position", {"lat": self._fmt_coord(lat), "lon": self._fmt_coord(lon)})
            geometry_type = "Loxodrome" if props.get("forceRhumbLine") else "Orthodrome"
            ET.SubElement(waypoint, f"{{{namespace}}}leg", {"starboardXTD": "0.03", "portsideXTD": "0.03", "geometryType": geometry_type})
            if isinstance(props.get("speed"), (int, float)):
                speeds_by_index[index] = float(props["speed"])
                has_schedule = True
            if isinstance(props.get("rpm"), (int, float)):
                rpms_by_index[index] = float(props["rpm"])
            time_attrs = {key: str(props[key]) for key in ("etd", "eta") if props.get(key)}
            if time_attrs:
                times_by_index[index] = time_attrs
                has_schedule = True

        if has_schedule:
            schedules = ET.SubElement(route, f"{{{namespace}}}schedules")
            schedule = ET.SubElement(schedules, f"{{{namespace}}}schedule", {"id": "0"})
            calculated = ET.SubElement(schedule, f"{{{namespace}}}calculated")
            for index in range(len(route_points)):
                attrs = {"waypointId": str(index)}
                if index in speeds_by_index:
                    attrs["speed"] = self._fmt_float(speeds_by_index[index])
                attrs.update(times_by_index.get(index, {}))
                ET.SubElement(calculated, f"{{{namespace}}}scheduleElement", attrs)

        if speeds_by_index or rpms_by_index:
            extensions = ET.SubElement(route, f"{{{namespace}}}extensions")
            extension = ET.SubElement(extensions, f"{{{namespace}}}extension")
            vo = ET.SubElement(extension, f"{{{namespace}}}VoyageOptimization")
            for index in sorted(set(speeds_by_index) | set(rpms_by_index)):
                attrs = {"waypointId": str(index), "usingspeed": "0"}
                if index in speeds_by_index:
                    attrs["speed"] = self._fmt_float(speeds_by_index[index])
                if index in rpms_by_index:
                    attrs["rpm"] = self._fmt_float(rpms_by_index[index])
                ET.SubElement(vo, f"{{{namespace}}}VOElement", attrs)

        ET.indent(route, space="    ")
        ET.ElementTree(route).write(output_path, encoding="UTF-8", xml_declaration=True)

    def _fmt_coord(self, value: float) -> str:
        return f"{value:.8f}".rstrip("0").rstrip(".")

    def _fmt_float(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._require_token()}", "Accept": "application/json"}

    def _get_request_json(self) -> Dict[str, Any]:
        raw = self.request_text.get("1.0", tk.END).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc

    def get_token(self) -> None:
        self._request_token_async()

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
                ws.settimeout(VESSEL_WS_RESPONSE_TIMEOUT_SECONDS)
                self.log("WebSocket connected. Sending request...")
                ws.send(json.dumps(payload))
                self.log(f"Request sent. Waiting for response up to {VESSEL_WS_RESPONSE_TIMEOUT_SECONDS} seconds...")
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
                        self._log_api_errors(parsed, "WebSocket response")
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
            except websocket.WebSocketTimeoutException as exc:
                self.log(f"ERROR: WebSocket response timed out after {VESSEL_WS_RESPONSE_TIMEOUT_SECONDS} seconds.")
                self.log("If the request still runs server-side, try loading the response by correlation/download URL when available.")
                messagebox.showerror("WebSocket Timeout", str(exc))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                messagebox.showerror("WebSocket Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()


class ProfileCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, title: str, y_label: str, color: str) -> None:
        super().__init__(master, background="#0f172a", highlightthickness=0, height=260)
        self.title = title
        self.y_label = y_label
        self.color = color
        self.intervals: List[Dict[str, Any]] = []
        self.series: List[Dict[str, Any]] = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_intervals(self, intervals: List[Dict[str, Any]]) -> None:
        self.set_series([{"label": self.title, "color": self.color, "intervals": intervals}] if intervals else [])

    def set_series(self, series: List[Dict[str, Any]]) -> None:
        normalized = []
        for index, item in enumerate(series, start=1):
            intervals = [
                interval
                for interval in item.get("intervals", [])
                if isinstance(interval.get("start"), datetime)
                and isinstance(interval.get("end"), datetime)
                and isinstance(interval.get("value"), (int, float))
            ]
            if not intervals:
                continue
            normalized.append(
                {
                    "label": str(item.get("label") or f"Series {index}"),
                    "color": str(item.get("color") or self.color),
                    "intervals": sorted(intervals, key=lambda interval: interval["start"]),
                }
            )
        self.series = normalized
        self.intervals = [interval for item in normalized for interval in item["intervals"]]
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self.create_rectangle(0, 0, width, height, fill="#0f172a", outline="")
        self.create_text(14, 12, anchor="nw", text=self.title, fill="#e2e8f0", font=("Segoe UI", 10, "bold"))

        if not self.intervals:
            self.create_text(width / 2, height / 2, text="No profile data", fill="#94a3b8", font=("Segoe UI", 10))
            return

        left, right, top, bottom = 68, 18, 42, 42
        plot_w = max(1, width - left - right)
        plot_h = max(1, height - top - bottom)
        start_time = min(item["start"] for item in self.intervals)
        end_time = max(item["end"] for item in self.intervals)
        values = [float(item["value"]) for item in self.intervals]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1
        else:
            padding = (max_value - min_value) * 0.08
            min_value -= padding
            max_value += padding
        total_seconds = max(1.0, (end_time - start_time).total_seconds())

        def x_pos(value: datetime) -> float:
            return left + ((value - start_time).total_seconds() / total_seconds) * plot_w

        def y_pos(value: float) -> float:
            return top + (max_value - value) / (max_value - min_value) * plot_h

        self.create_line(left, top, left, top + plot_h, fill="#64748b")
        self.create_line(left, top + plot_h, left + plot_w, top + plot_h, fill="#64748b")

        for index in range(5):
            fraction = index / 4
            value = min_value + (max_value - min_value) * (1 - fraction)
            y = top + plot_h * fraction
            self.create_line(left, y, left + plot_w, y, fill="#1e293b")
            self.create_text(left - 8, y, anchor="e", text=f"{value:.1f}", fill="#cbd5e1", font=("Segoe UI", 8))

        for index in range(5):
            fraction = index / 4
            tick_time = start_time + timedelta(seconds=total_seconds * fraction)
            x = left + plot_w * fraction
            self.create_line(x, top, x, top + plot_h, fill="#1e293b")
            self.create_text(x, top + plot_h + 16, text=tick_time.strftime("%m-%d %H:%M"), fill="#cbd5e1", font=("Segoe UI", 8))

        for item in self.series:
            previous_x = None
            previous_y = None
            color = item["color"]
            for interval in item["intervals"]:
                x1 = x_pos(interval["start"])
                x2 = x_pos(interval["end"])
                y = y_pos(float(interval["value"]))
                if previous_x is not None and previous_y is not None:
                    self.create_line(x1, previous_y, x1, y, fill=color, width=2)
                self.create_line(x1, y, x2, y, fill=color, width=2)
                previous_x = x2
                previous_y = y

        self.create_text(16, top + plot_h / 2, text=self.y_label, fill="#cbd5e1", font=("Segoe UI", 8), angle=90)
        self._draw_legend(width)

    def _draw_legend(self, width: int) -> None:
        if not self.series:
            return
        max_items = 6
        y = 16
        x = width - 16
        for item in self.series[:max_items]:
            label = self._short_label(item["label"])
            text_id = self.create_text(x, y, anchor="ne", text=label, fill="#cbd5e1", font=("Segoe UI", 8))
            bbox = self.bbox(text_id)
            line_x = (bbox[0] - 22) if bbox else (x - 96)
            self.create_line(line_x, y, line_x + 14, y, fill=item["color"], width=3)
            y += 15
        if len(self.series) > max_items:
            self.create_text(x, y, anchor="ne", text=f"+{len(self.series) - max_items} more", fill="#94a3b8", font=("Segoe UI", 8))

    def _short_label(self, label: str) -> str:
        return label if len(label) <= 30 else f"{label[:27]}..."


class OptimalProfileFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.speed_intervals: List[Dict[str, Any]] = []
        self.rpm_intervals: List[Dict[str, Any]] = []
        self.profile_series: List[Dict[str, Any]] = []
        self._init_log_queue()
        self._build_ui()
        self.after(100, self._flush_log_queue)

    def _build_ui(self) -> None:
        controls = ttk.LabelFrame(self, text="Result RTZ Folders")
        controls.pack(fill=tk.X, pady=(0, 8))
        self.profile_folder_var = tk.StringVar(value=str(DEFAULT_CONTINUOUS_OUTPUT_DIR))
        self.profile_limit_var = tk.StringVar(value="0")
        ttk.Label(controls, text="Folders").grid(row=0, column=0, sticky="nw", padx=5, pady=5)
        self.profile_folder_list = tk.Listbox(controls, height=4, exportselection=False)
        self.profile_folder_list.grid(row=0, column=1, rowspan=3, sticky="we", padx=5, pady=5)
        self.profile_folder_list.insert(tk.END, str(DEFAULT_CONTINUOUS_OUTPUT_DIR))
        folder_buttons = ttk.Frame(controls)
        folder_buttons.grid(row=0, column=2, rowspan=3, sticky="n", padx=5, pady=5)
        ttk.Button(folder_buttons, text="Add Folder", command=self.browse_profile_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(folder_buttons, text="Remove", command=self.remove_profile_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(folder_buttons, text="Clear", command=self.clear_profile_folders).pack(fill=tk.X)
        ttk.Label(controls, text="Max files (0 = all)").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        ttk.Spinbox(controls, textvariable=self.profile_limit_var, from_=0, to=10000, increment=1, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=5)
        ttk.Button(controls, text="Generate Profiles", command=self.generate_profiles).grid(row=3, column=1, sticky="e", padx=5, pady=5)
        ttk.Button(controls, text="Save CSV", command=self.save_profiles_csv).grid(row=3, column=2, padx=5, pady=5)
        controls.columnconfigure(1, weight=1)

        charts = ttk.PanedWindow(self, orient=tk.VERTICAL)
        charts.pack(fill=tk.BOTH, expand=True)
        speed_frame = ttk.LabelFrame(charts, text="Speed Profile")
        rpm_frame = ttk.LabelFrame(charts, text="RPM Profile")
        charts.add(speed_frame, weight=1)
        charts.add(rpm_frame, weight=1)
        self.speed_canvas = ProfileCanvas(speed_frame, "Speed Profile", "Speed (kn)", "#38bdf8")
        self.speed_canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.rpm_canvas = ProfileCanvas(rpm_frame, "RPM Profile", "RPM", "#f59e0b")
        self.rpm_canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        log_frame = ttk.LabelFrame(self, text="Profile Log")
        log_frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def browse_profile_folder(self) -> None:
        path = filedialog.askdirectory(title="Select result RTZ folder", initialdir=str(DEFAULT_CONTINUOUS_BATCH_ROOT))
        if path:
            self._add_profile_folder(path)

    def remove_profile_folder(self) -> None:
        for index in reversed(self.profile_folder_list.curselection()):
            self.profile_folder_list.delete(index)

    def clear_profile_folders(self) -> None:
        self.profile_folder_list.delete(0, tk.END)

    def _add_profile_folder(self, path: str) -> None:
        value = str(Path(path))
        existing = set(self.profile_folder_list.get(0, tk.END))
        if value not in existing:
            self.profile_folder_list.insert(tk.END, value)

    def _profile_folders(self) -> List[Path]:
        values = [str(value).strip() for value in self.profile_folder_list.get(0, tk.END) if str(value).strip()]
        if not values and self.profile_folder_var.get().strip():
            values = [self.profile_folder_var.get().strip()]
        folders = []
        seen = set()
        for value in values:
            folder = Path(value)
            key = str(folder).lower()
            if key in seen:
                continue
            seen.add(key)
            folders.append(folder)
        return folders

    def generate_profiles(self) -> None:
        threading.Thread(target=self._generate_profiles_worker, daemon=True).start()

    def _generate_profiles_worker(self) -> None:
        try:
            folders = self._profile_folders()
            if not folders:
                raise ValueError("Add at least one result RTZ folder.")
            limit = self._profile_limit()
            used_labels: set = set()
            profile_series: List[Dict[str, Any]] = []
            for folder in folders:
                if not folder.exists():
                    raise ValueError(f"Folder not found: {folder}")
                files = self._optimal_rtz_files(folder)
                if limit:
                    files = files[:limit]
                if len(files) < 2:
                    self.log(f"Skipping folder with fewer than two RTZ files: {folder}")
                    continue

                records_by_kind: Dict[str, List[Dict[str, Any]]] = {}
                for path in files:
                    metadata = self._rtz_file_metadata(path)
                    timestamp = metadata.get("timestamp_dt")
                    if timestamp is None:
                        self.log(f"Skipping file without result timestamp pattern: {path.name}")
                        continue
                    points = self._parse_profile_rtz(path)
                    if not points:
                        self.log(f"Skipping RTZ without waypoints: {path.name}")
                        continue
                    kind = str(metadata.get("kind") or "Result")
                    records_by_kind.setdefault(kind, []).append({"path": path, "time": timestamp, "points": points, "kind": kind})

                usable_groups: List[Tuple[str, List[Dict[str, Any]]]] = []
                for kind, records in sorted(records_by_kind.items(), key=lambda item: item[0].lower()):
                    records.sort(key=lambda item: (item["time"], item["path"].name))
                    if len(records) < 2:
                        self.log(f"Skipping {kind} in {folder.name}: fewer than two timestamped RTZ files.")
                        continue
                    usable_groups.append((kind, records))
                if not usable_groups:
                    self.log(f"Skipping folder without usable timestamped result RTZ pairs: {folder}")
                    continue

                show_kind_in_label = len(usable_groups) > 1
                for kind, records in usable_groups:
                    label = self._profile_series_label(folder, used_labels, kind if show_kind_in_label else "")
                    color = PROFILE_SERIES_COLORS[len(profile_series) % len(PROFILE_SERIES_COLORS)]
                    speed_intervals: List[Dict[str, Any]] = []
                    rpm_intervals: List[Dict[str, Any]] = []
                    for index in range(len(records) - 1):
                        speed_items, rpm_items = self._profile_pair_intervals(records[index], records[index + 1])
                        for item in speed_items:
                            item["series"] = label
                            item["folder"] = str(folder)
                            item["kind"] = kind
                        for item in rpm_items:
                            item["series"] = label
                            item["folder"] = str(folder)
                            item["kind"] = kind
                        speed_intervals.extend(speed_items)
                        rpm_intervals.extend(rpm_items)

                    profile_series.append(
                        {
                            "label": label,
                            "folder": str(folder),
                            "kind": kind,
                            "color": color,
                            "records": records,
                            "speed_intervals": speed_intervals,
                            "rpm_intervals": rpm_intervals,
                        }
                    )

            if not profile_series:
                raise ValueError("No usable result RTZ folders found.")

            self.after(0, lambda: self._apply_profiles(profile_series))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.after(0, lambda: messagebox.showerror("Result Preview Error", str(exc)))

    def _profile_series_label(self, folder: Path, used_labels: set, kind: str = "") -> str:
        base = folder.name or str(folder)
        if kind:
            base = f"{base} / {kind}"
        label = base
        if label in used_labels:
            parent_label = f"{folder.parent.name}\\{base}" if folder.parent.name else base
            label = parent_label
        suffix = 2
        while label in used_labels:
            label = f"{base} {suffix}"
            suffix += 1
        used_labels.add(label)
        return label

    def _apply_profiles(self, profile_series: List[Dict[str, Any]]) -> None:
        self.profile_series = profile_series
        self.speed_intervals = [item for series in profile_series for item in series["speed_intervals"]]
        self.rpm_intervals = [item for series in profile_series for item in series["rpm_intervals"]]
        self.speed_canvas.set_series(
            [
                {"label": series["label"], "color": series["color"], "intervals": series["speed_intervals"]}
                for series in profile_series
            ]
        )
        self.rpm_canvas.set_series(
            [
                {"label": series["label"], "color": series["color"], "intervals": series["rpm_intervals"]}
                for series in profile_series
            ]
        )
        total_records = sum(len(series["records"]) for series in profile_series)
        self.log(
            f"Generated profiles from {len(profile_series)} result series and {total_records} RTZ files: "
            f"{len(self.speed_intervals)} speed intervals, {len(self.rpm_intervals)} rpm intervals."
        )
        for series in profile_series:
            self.log(
                f"{series['label']}: {len(series['records'])} files, "
                f"{len(series['speed_intervals'])} speed intervals, {len(series['rpm_intervals'])} rpm intervals."
            )
        if self.speed_intervals:
            speeds = [float(item["value"]) for item in self.speed_intervals]
            self.log(f"Speed min/avg/max: {min(speeds):.2f}/{sum(speeds) / len(speeds):.2f}/{max(speeds):.2f} kn")
        if self.rpm_intervals:
            rpms = [float(item["value"]) for item in self.rpm_intervals]
            self.log(f"RPM min/avg/max: {min(rpms):.2f}/{sum(rpms) / len(rpms):.2f}/{max(rpms):.2f}")

    def save_profiles_csv(self) -> None:
        if not self.speed_intervals and not self.rpm_intervals:
            messagebox.showinfo("Result Preview", "Generate profiles before saving CSV.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Profile CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["profile", "series", "kind", "folder", "start_utc", "end_utc", "value", "source_file", "leg_index", "distance_nm"])
            for series in self.profile_series:
                for profile_name, intervals in (("speed", series["speed_intervals"]), ("rpm", series["rpm_intervals"])):
                    for item in intervals:
                        writer.writerow(
                            [
                                profile_name,
                                item.get("series", series.get("label", "")),
                                item.get("kind", series.get("kind", "")),
                                item.get("folder", series.get("folder", "")),
                                _utc_z(item["start"]),
                                _utc_z(item["end"]),
                                self._fmt_float(float(item["value"])),
                                item.get("source", ""),
                                item.get("leg_index", ""),
                                self._fmt_float(float(item.get("distance_nm", 0))),
                            ]
                        )
        self.log(f"Profile CSV saved: {path}")

    def _profile_limit(self) -> int:
        try:
            value = int(self.profile_limit_var.get())
        except ValueError:
            return 0
        return max(0, value)

    def _optimal_rtz_files(self, folder: Path) -> List[Path]:
        return sorted(folder.glob("*.rtz"), key=lambda path: (self._rtz_file_metadata(path).get("timestamp_sort") or path.name, path.name))

    def _rtz_file_metadata(self, path: Path) -> Dict[str, Any]:
        match = RTZ_FILE_NAME_RE.match(path.name)
        if not match:
            return {"timestamp_sort": path.name, "timestamp_dt": None}
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "kind": match.group("kind"),
            "timestamp_sort": dt,
            "timestamp_dt": dt,
            "timestamp_label": f"{match.group('date')}_{match.group('time')}",
        }

    def _parse_profile_rtz(self, path: Path) -> List[Dict[str, Any]]:
        root = ET.parse(path).getroot()
        namespace = {"rtz": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        waypoint_path = ".//rtz:waypoint" if namespace else ".//waypoint"
        position_path = "rtz:position" if namespace else "position"
        schedule_paths = (
            [".//rtz:scheduleElement", ".//rtz:sheduleElement"] if namespace else [".//scheduleElement", ".//sheduleElement"]
        )
        vo_path = ".//rtz:VOElement" if namespace else ".//VOElement"
        data_by_waypoint: Dict[str, Dict[str, Any]] = {}

        for schedule_path in schedule_paths:
            for item in root.findall(schedule_path, namespace):
                waypoint_id = item.attrib.get("waypointId")
                if not waypoint_id:
                    continue
                waypoint_data = data_by_waypoint.setdefault(waypoint_id, {})
                for key in ("speed", "rpm"):
                    if item.attrib.get(key):
                        waypoint_data[key] = float(item.attrib[key])
                for key in ("etd", "eta"):
                    if item.attrib.get(key):
                        waypoint_data[key] = item.attrib[key]

        for item in root.findall(vo_path, namespace):
            waypoint_id = item.attrib.get("waypointId")
            if not waypoint_id:
                continue
            waypoint_data = data_by_waypoint.setdefault(waypoint_id, {})
            for key in ("speed", "rpm"):
                if item.attrib.get(key):
                    waypoint_data[key] = float(item.attrib[key])

        points = []
        for waypoint in root.findall(waypoint_path, namespace):
            position = waypoint.find(position_path, namespace)
            if position is None:
                continue
            waypoint_id = waypoint.attrib.get("id") or str(len(points))
            leg = waypoint.find("rtz:leg", namespace) if namespace else waypoint.find("leg")
            properties = {
                "name": waypoint.attrib.get("name") or f"WP {waypoint_id}",
                "forceRhumbLine": leg is not None and leg.attrib.get("geometryType") == "Loxodrome",
            }
            properties.update(data_by_waypoint.get(waypoint_id, {}))
            points.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {"type": "Point", "coordinates": [float(position.attrib["lon"]), float(position.attrib["lat"])]},
                }
            )
        _use_next_waypoint_speed_rpm(points)
        return points

    def _profile_pair_intervals(
        self,
        current: Dict[str, Any],
        next_record: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        start_time: datetime = current["time"]
        end_time: datetime = next_record["time"]
        interval_seconds = max(1.0, (end_time - start_time).total_seconds())
        segments = self._used_segments_until(current["points"], next_record["points"][0])
        if not segments:
            first_props = current["points"][0].get("properties", {})
            segments = [
                {
                    "leg_index": 0,
                    "distance_nm": 0.0,
                    "speed": first_props.get("speed"),
                    "rpm": first_props.get("rpm"),
                    "source": current["path"].name,
                }
            ]

        fallback_speed = self._average_value(segments, "speed") or 10.0
        raw_durations = []
        for segment in segments:
            speed = segment.get("speed") if isinstance(segment.get("speed"), (int, float)) and segment.get("speed") > 0 else fallback_speed
            raw_durations.append(max(0.0, float(segment.get("distance_nm", 0))) / float(speed) * 3600)
        total_raw = sum(raw_durations)
        if total_raw <= 0:
            raw_durations = [interval_seconds / len(segments)] * len(segments)
            total_raw = interval_seconds
        scale = interval_seconds / total_raw

        speed_intervals: List[Dict[str, Any]] = []
        rpm_intervals: List[Dict[str, Any]] = []
        cursor = start_time
        for index, segment in enumerate(segments):
            if index == len(segments) - 1:
                segment_end = end_time
            else:
                segment_end = cursor + timedelta(seconds=raw_durations[index] * scale)
            if segment_end <= cursor:
                continue
            base = {
                "start": cursor,
                "end": segment_end,
                "source": current["path"].name,
                "leg_index": segment.get("leg_index", index),
                "distance_nm": segment.get("distance_nm", 0.0),
            }
            if isinstance(segment.get("speed"), (int, float)):
                speed_intervals.append({**base, "value": float(segment["speed"])})
            if isinstance(segment.get("rpm"), (int, float)):
                rpm_intervals.append({**base, "value": float(segment["rpm"])})
            cursor = segment_end
        return speed_intervals, rpm_intervals

    def _used_segments_until(self, points: List[Dict[str, Any]], target: Dict[str, Any]) -> List[Dict[str, Any]]:
        if len(points) < 2:
            return []
        target_lat, target_lng = self._feature_lat_lng(target)
        best_index = 0
        best_fraction = 0.0
        best_score = float("inf")
        for index in range(len(points) - 1):
            start_lat, start_lng = self._feature_lat_lng(points[index])
            end_lat, end_lng = self._feature_lat_lng(points[index + 1])
            score, fraction = self._point_segment_projection(target_lat, target_lng, start_lat, start_lng, end_lat, end_lng)
            if score < best_score:
                best_score = score
                best_index = index
                best_fraction = fraction

        segments = []
        for index in range(best_index):
            segments.append(self._segment_profile(points, index, 1.0))
        if best_fraction > 1e-6:
            segments.append(self._segment_profile(points, best_index, best_fraction))
        return [segment for segment in segments if segment["distance_nm"] > 1e-6 or segment.get("speed") or segment.get("rpm")]

    def _segment_profile(self, points: List[Dict[str, Any]], index: int, fraction: float) -> Dict[str, Any]:
        start = points[index]
        end = points[index + 1]
        start_props = start.get("properties", {}) if isinstance(start.get("properties"), dict) else {}
        end_props = end.get("properties", {}) if isinstance(end.get("properties"), dict) else {}
        return {
            "leg_index": index,
            "distance_nm": self._distance_nm(start, end) * max(0.0, min(1.0, fraction)),
            "speed": start_props.get("speed") if isinstance(start_props.get("speed"), (int, float)) else end_props.get("speed"),
            "rpm": start_props.get("rpm") if isinstance(start_props.get("rpm"), (int, float)) else end_props.get("rpm"),
        }

    def _point_segment_projection(
        self,
        point_lat: float,
        point_lng: float,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
    ) -> Tuple[float, float]:
        scale = math.cos(math.radians((point_lat + start_lat + end_lat) / 3))
        end_lng = start_lng + ((end_lng - start_lng + 180) % 360) - 180
        point_lng = start_lng + ((point_lng - start_lng + 180) % 360) - 180
        px, py = point_lng * scale, point_lat
        sx, sy = start_lng * scale, start_lat
        ex, ey = end_lng * scale, end_lat
        dx, dy = ex - sx, ey - sy
        if dx == 0 and dy == 0:
            return (px - sx) ** 2 + (py - sy) ** 2, 0.0
        fraction = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)))
        closest_x = sx + fraction * dx
        closest_y = sy + fraction * dy
        return (px - closest_x) ** 2 + (py - closest_y) ** 2, fraction

    def _feature_lat_lng(self, feature: Dict[str, Any]) -> Tuple[float, float]:
        coordinates = feature["geometry"]["coordinates"]
        return float(coordinates[1]), float(coordinates[0])

    def _distance_nm(self, first: Dict[str, Any], second: Dict[str, Any]) -> float:
        lat1, lng1 = self._feature_lat_lng(first)
        lat2, lng2 = self._feature_lat_lng(second)
        radius_nm = 3440.065
        d_lat = math.radians(lat2 - lat1)
        d_lng = math.radians(((lng2 - lng1 + 180) % 360) - 180)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
        return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))

    def _average_value(self, items: List[Dict[str, Any]], key: str) -> Optional[float]:
        values = [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]
        return sum(values) / len(values) if values else None

    def _fmt_float(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")


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
        self._request_token_async()

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
        self._request_token_async()

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
        self._request_token_async()

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
                            self._log_api_errors(parsed, "WebSocket response")
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
        self._request_token_sync("Refreshing token...")

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


class GlobeCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background="#07111f", highlightthickness=0)
        self.points: List[Dict[str, Any]] = []
        self.lines: List[List[Dict[str, float]]] = []
        self.routes: List[Dict[str, Any]] = []
        self.yaw = math.radians(-25)
        self.pitch = math.radians(12)
        self.zoom = 1.0
        self._drag_start: Optional[Tuple[int, int, float, float]] = None
        self._cx = 0.0
        self._cy = 0.0
        self._radius = 1.0
        self._earth_texture: Optional[Any] = None
        self._earth_texture_status = "Loading NASA Blue Marble satellite texture..."
        self._earth_texture_loading = False
        self._globe_photo: Optional[Any] = None

        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<ButtonPress-1>", self._start_drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda _event: self._zoom_by(1.12))
        self.bind("<Button-5>", lambda _event: self._zoom_by(1 / 1.12))
        self._load_earth_texture_async()

    def render_map_data(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        routes: List[Dict[str, Any]],
    ) -> None:
        self.points = list(points)
        self.lines = list(lines)
        self.routes = list(routes)
        self.redraw()

    def clear_globe(self) -> None:
        self.points = []
        self.lines = []
        self.routes = []
        self.redraw()

    def reset_view(self) -> None:
        self.yaw = math.radians(-25)
        self.pitch = math.radians(12)
        self.zoom = 1.0
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self._cx = width / 2
        self._cy = height / 2
        self._radius = max(80.0, min(420.0, min(width, height) * 0.43 * self.zoom))

        self.create_rectangle(0, 0, width, height, fill="#07111f", outline="")
        self._draw_satellite_globe()
        self._draw_lines()
        self._draw_routes()
        self._draw_points()
        self._draw_overlay(width, height)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_start = (int(event.x), int(event.y), self.yaw, self.pitch)

    def _drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        start_x, start_y, start_yaw, start_pitch = self._drag_start
        self.yaw = start_yaw + (int(event.x) - start_x) * 0.01
        self.pitch = max(math.radians(-75), min(math.radians(75), start_pitch + (int(event.y) - start_y) * 0.01))
        self.redraw()

    def _wheel(self, event: tk.Event) -> None:
        self._zoom_by(1.12 if int(event.delta) > 0 else 1 / 1.12)

    def _zoom_by(self, factor: float) -> None:
        self.zoom = max(0.55, min(2.4, self.zoom * factor))
        self.redraw()

    def _draw_satellite_globe(self) -> None:
        if Image is None or ImageTk is None:
            self._draw_texture_fallback("Install Pillow to render the satellite globe.")
            return
        if self._earth_texture is None:
            self._draw_texture_fallback(self._earth_texture_status)
            return

        diameter = max(120, int(self._radius * 2))
        globe = self._render_textured_globe(diameter)
        self._globe_photo = ImageTk.PhotoImage(globe)
        self.create_image(self._cx, self._cy, image=self._globe_photo)
        self.create_oval(
            self._cx - self._radius,
            self._cy - self._radius,
            self._cx + self._radius,
            self._cy + self._radius,
            outline="#93c5fd",
            width=1,
        )

    def _draw_texture_fallback(self, message: str) -> None:
        self.create_oval(
            self._cx - self._radius,
            self._cy - self._radius,
            self._cx + self._radius,
            self._cy + self._radius,
            fill="#0f2742",
            outline="#38bdf8",
            width=2,
        )
        self.create_text(
            self._cx,
            self._cy,
            text=message,
            fill="#cbd5e1",
            font=("Segoe UI", 9),
            width=max(180, int(self._radius * 1.5)),
        )

    def _render_textured_globe(self, diameter: int) -> Any:
        texture = self._earth_texture
        radius = diameter / 2
        image = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        pixels = image.load()
        texture_pixels = texture.load()
        tex_w, tex_h = texture.size
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        cos_pitch = math.cos(self.pitch)
        sin_pitch = math.sin(self.pitch)

        for py in range(diameter):
            ny = (radius - py - 0.5) / radius
            for px in range(diameter):
                nx = (px + 0.5 - radius) / radius
                distance_sq = nx * nx + ny * ny
                if distance_sq > 1:
                    continue
                nz = math.sqrt(max(0.0, 1.0 - distance_sq))

                world_y = ny * cos_pitch + nz * sin_pitch
                z1 = -ny * sin_pitch + nz * cos_pitch
                world_x = nx * cos_yaw - z1 * sin_yaw
                world_z = nx * sin_yaw + z1 * cos_yaw

                lat = math.asin(max(-1.0, min(1.0, world_y)))
                lng = math.atan2(world_x, world_z)
                tx = int(((lng + math.pi) / (2 * math.pi)) * tex_w) % tex_w
                ty = max(0, min(tex_h - 1, int(((math.pi / 2 - lat) / math.pi) * tex_h)))
                red, green, blue = texture_pixels[tx, ty][:3]
                shade = 0.58 + 0.42 * nz
                pixels[px, py] = (int(red * shade), int(green * shade), int(blue * shade), 255)
        return image

    def _load_earth_texture_async(self) -> None:
        if Image is None or self._earth_texture_loading:
            return
        self._earth_texture_loading = True

        def worker() -> None:
            try:
                texture = self._load_earth_texture()
                self.after(0, lambda: self._set_earth_texture(texture, "NASA Blue Marble satellite texture loaded."))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda: self._set_earth_texture_error(message))

        threading.Thread(target=worker, daemon=True).start()

    def _load_earth_texture(self) -> Any:
        cache_path = self._earth_texture_cache_path()
        if cache_path.exists():
            return Image.open(cache_path).convert("RGB")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(EARTH_TEXTURE_URL, timeout=60)
        response.raise_for_status()
        texture = Image.open(BytesIO(response.content)).convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        texture = texture.resize((EARTH_TEXTURE_WIDTH, EARTH_TEXTURE_HEIGHT), resample)
        texture.save(cache_path, "JPEG", quality=90)
        return texture

    def _earth_texture_cache_path(self) -> Path:
        base = Path(os.getenv("LOCALAPPDATA") or Path.home())
        return base / "ABBApiGui" / EARTH_TEXTURE_CACHE_NAME

    def _set_earth_texture(self, texture: Any, status: str) -> None:
        self._earth_texture = texture
        self._earth_texture_status = status
        self._earth_texture_loading = False
        self.redraw()

    def _set_earth_texture_error(self, message: str) -> None:
        self._earth_texture_status = f"Satellite texture unavailable: {message}"
        self._earth_texture_loading = False
        self.redraw()

    def _draw_lines(self) -> None:
        for line in self.lines:
            self._draw_geo_path(line, "#00d4ff", 2)

    def _draw_routes(self) -> None:
        for route_index, route in enumerate(self.routes, start=1):
            route_points = route.get("points", [])
            if len(route_points) < 2:
                continue
            has_speed = any(isinstance(point.get("speed"), (int, float)) for point in route_points)
            if has_speed:
                for start, end in zip(route_points, route_points[1:]):
                    speed = end.get("speed") if isinstance(end.get("speed"), (int, float)) else start.get("speed")
                    self._draw_geo_path([start, end], self._speed_color(speed), 3)
            else:
                self._draw_geo_path(route_points, "#00d4ff", 3)
            self._draw_route_nodes(route_index, route_points)

    def _draw_points(self) -> None:
        label_limit = 80
        for index, point in enumerate(self.points, start=1):
            label = str(point.get("label") or f"Port {index}")
            self._draw_marker(point, "#38bdf8", 5, label if index <= label_limit else "")

    def _draw_route_nodes(self, route_index: int, route_points: List[Dict[str, Any]]) -> None:
        max_nodes = 260
        step = max(1, len(route_points) // max_nodes)
        last_index = len(route_points) - 1
        for index, point in enumerate(route_points):
            endpoint = index in {0, last_index}
            if not endpoint and index % step != 0:
                continue
            if index == 0:
                label = f"R{route_index} START"
                color = "#f8fafc"
                radius = 6
            elif index == last_index:
                label = f"R{route_index} END"
                color = "#f8fafc"
                radius = 6
            else:
                label = f"WP {index + 1}" if len(route_points) <= 80 else ""
                color = "#94a3b8"
                radius = 3
            self._draw_marker(point, color, radius, label)

    def _draw_geo_path(
        self,
        path: List[Dict[str, Any]],
        color: str,
        width: int,
        samples_per_segment: Optional[int] = None,
        close_path: bool = False,
    ) -> None:
        path = self._prepared_path(path, close_path)
        if len(path) < 2:
            return
        if samples_per_segment is None:
            samples_per_segment = 24 if len(path) <= 10 else 1

        for start, end in zip(path, path[1:]):
            start_vec = self._latlng_to_vector(float(start["lat"]), float(start["lng"]))
            end_vec = self._latlng_to_vector(float(end["lat"]), float(end["lng"]))
            last_xy: Optional[Tuple[float, float]] = None
            for index in range(samples_per_segment + 1):
                point_vec = self._slerp(start_vec, end_vec, index / samples_per_segment)
                xy = self._project_vector(point_vec)
                if xy is None:
                    last_xy = None
                    continue
                if last_xy is not None:
                    self.create_line(last_xy[0], last_xy[1], xy[0], xy[1], fill=color, width=width, smooth=True)
                last_xy = xy

    def _prepared_path(self, path: List[Dict[str, Any]], close_path: bool) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for point in path:
            if prepared and self._same_geo_point(prepared[-1], point):
                continue
            prepared.append(point)
        if len(prepared) > 1 and self._same_geo_point(prepared[0], prepared[-1]):
            prepared = prepared[:-1]
        if close_path and len(prepared) > 2:
            prepared = prepared + [prepared[0]]
        return prepared

    def _same_geo_point(self, first: Dict[str, Any], second: Dict[str, Any]) -> bool:
        lat_delta = abs(float(first["lat"]) - float(second["lat"]))
        lng_delta = abs(((float(first["lng"]) - float(second["lng"]) + 180) % 360) - 180)
        return lat_delta < 1e-7 and lng_delta < 1e-7

    def _draw_marker(self, point: Dict[str, Any], color: str, radius: int, label: str = "") -> None:
        xy = self._project_latlng(float(point["lat"]), float(point["lng"]))
        if xy is None:
            return
        x, y = xy
        self.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="#0f172a", width=1)
        if label:
            self.create_text(
                x + radius + 4,
                y - radius - 2,
                anchor="w",
                text=label[:36],
                fill="#e2e8f0",
                font=("Segoe UI", 8),
            )

    def _draw_overlay(self, width: int, height: int) -> None:
        route_nodes = sum(len(route.get("points", [])) for route in self.routes)
        summary = f"3D Satellite Globe  Ports {len(self.points)}  Lines {len(self.lines)}  Routes {len(self.routes)}  Nodes {route_nodes}"
        self.create_text(14, 12, anchor="nw", text=summary, fill="#e2e8f0", font=("Segoe UI", 9, "bold"))
        legend = [
            ("< 8 kn", "#2563eb"),
            ("8-12 kn", "#16a34a"),
            ("12-16 kn", "#f59e0b"),
            (">= 16 kn", "#dc2626"),
            ("no speed", "#00d4ff"),
        ]
        x = 14
        y = max(42, height - 22)
        for label, color in legend:
            self.create_line(x, y, x + 22, y, fill=color, width=4)
            self.create_text(x + 28, y, anchor="w", text=label, fill="#cbd5e1", font=("Segoe UI", 8))
            x += 86

    def _project_latlng(self, lat: float, lng: float) -> Optional[Tuple[float, float]]:
        return self._project_vector(self._latlng_to_vector(lat, lng))

    def _project_vector(self, vector: Tuple[float, float, float]) -> Optional[Tuple[float, float]]:
        x, y, z = vector
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        x1 = x * cos_yaw + z * sin_yaw
        z1 = -x * sin_yaw + z * cos_yaw

        cos_pitch = math.cos(self.pitch)
        sin_pitch = math.sin(self.pitch)
        y2 = y * cos_pitch - z1 * sin_pitch
        z2 = y * sin_pitch + z1 * cos_pitch
        if z2 <= 0:
            return None
        return self._cx + self._radius * x1, self._cy - self._radius * y2

    def _latlng_to_vector(self, lat: float, lng: float) -> Tuple[float, float, float]:
        lat_rad = math.radians(lat)
        lng_rad = math.radians(lng)
        cos_lat = math.cos(lat_rad)
        return (cos_lat * math.sin(lng_rad), math.sin(lat_rad), cos_lat * math.cos(lng_rad))

    def _slerp(
        self,
        start: Tuple[float, float, float],
        end: Tuple[float, float, float],
        fraction: float,
    ) -> Tuple[float, float, float]:
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(start, end))))
        if dot > 0.9995 or dot < -0.9995:
            return self._normalize(tuple(start[i] + (end[i] - start[i]) * fraction for i in range(3)))
        omega = math.acos(dot)
        sin_omega = math.sin(omega)
        if abs(sin_omega) < 1e-8:
            return start
        start_scale = math.sin((1 - fraction) * omega) / sin_omega
        end_scale = math.sin(fraction * omega) / sin_omega
        return tuple(start[i] * start_scale + end[i] * end_scale for i in range(3))

    def _normalize(self, vector: Tuple[float, float, float]) -> Tuple[float, float, float]:
        length = math.sqrt(sum(value * value for value in vector))
        if length <= 1e-9:
            return (0.0, 0.0, 1.0)
        return tuple(value / length for value in vector)

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


class MapPreviewFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.source_data: Optional[Any] = None
        self._last_extract_stats: Dict[str, int] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        controls = ttk.LabelFrame(self, text="In-App 3D Globe Preview")
        controls.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(controls, text="Load Active Request", command=self.load_active_request).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(controls, text="Load Active Response", command=self.load_active_response).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(controls, text="Show In App", command=self.show_map_preview).grid(row=0, column=2, padx=5, pady=5)
        ttk.Label(controls, text="Max markers").grid(row=0, column=3, sticky="e", padx=5, pady=5)
        self.max_markers_var = tk.StringVar(value="1000")
        ttk.Spinbox(controls, textvariable=self.max_markers_var, from_=100, to=20000, increment=100, width=10).grid(row=0, column=4, sticky="w", padx=5, pady=5)
        controls.columnconfigure(5, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        source_frame = ttk.LabelFrame(main, text="Map Source JSON")
        right_frame = ttk.Frame(main)
        main.add(source_frame, weight=2)
        main.add(right_frame, weight=3)

        self.source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.NONE)
        self.source_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        map_frame = ttk.LabelFrame(right_frame, text="3D Globe")
        map_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.globe_widget = GlobeCanvas(map_frame)
        self.globe_widget.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        result_frame = ttk.LabelFrame(right_frame, text="Extracted Map Data / Log")
        result_frame.pack(fill=tk.BOTH, expand=False)
        self.result_text = scrolledtext.ScrolledText(result_frame, wrap=tk.NONE)
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Clear", command=self.clear).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Reset Globe", command=self.reset_map).pack(side=tk.LEFT, padx=5)

    def clear(self) -> None:
        self.source_data = None
        self.source_text.delete("1.0", tk.END)
        self.result_text.delete("1.0", tk.END)
        self.globe_widget.clear_globe()

    def reset_map(self) -> None:
        self.globe_widget.reset_view()

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
            self._render_in_app_globe(points, lines, routes)
            stats = self._last_extract_stats
            self._log(f"Points rendered: {len(points)} / found: {stats.get('points_found', len(points))}")
            if stats.get("points_sampled", 0):
                self._log(f"Large point set sampled to max markers: {max_markers}")
            self._log(f"Lines: {len(lines)}")
            self._log(f"Routes: {len(routes)} / route nodes: {stats.get('route_nodes_found', 0)}")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            messagebox.showerror("Map Preview Error", str(exc))

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

        def same_point(first: Dict[str, float], second: Dict[str, float]) -> bool:
            lat_delta = abs(first["lat"] - second["lat"])
            lng_delta = abs(((first["lng"] - second["lng"] + 180) % 360) - 180)
            return lat_delta < 1e-7 and lng_delta < 1e-7

        def open_path(path: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if len(path) > 1 and same_point(path[0], path[-1]):
                return path[:-1]
            return path

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
            line = open_path(line)
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
            route_points = open_path(route_points)
            if len(route_points) < 2:
                return False
            stats["route_nodes_found"] += len(route_points)
            if len(route_points) > max_line_points:
                step = max(1, len(route_points) // max_line_points)
                route_points = route_points[::step][:max_line_points]
                stats["line_points_trimmed"] += 1
            if len(route_points) <= 10:
                for route_point in route_points:
                    add_point(
                        {"lat": route_point["lat"], "lng": route_point["lng"]},
                        str(route_point.get("label") or label),
                    )
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

    def _render_in_app_globe(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        routes: List[Dict[str, Any]],
    ) -> None:
        self.globe_widget.render_map_data(points, lines, routes)
        for route_index, route in enumerate(routes, start=1):
            route_points = route.get("points", [])
            if len(route_points) >= 2:
                self._log_speed_profile(route_index, str(route.get("label") or "Route"), route_points)

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
        self.profile_tab = OptimalProfileFrame(self.tabs)
        self.api_tabs = {self.vessel_tab, self.voyage_tab, self.product_tab, self.notification_tab}
        self.last_api_tab = self.vessel_tab
        self.tabs.add(self.vessel_tab, text="Vessel Routing API")
        self.tabs.add(self.voyage_tab, text="Voyage Configuration API")
        self.tabs.add(self.product_tab, text="Product API")
        self.tabs.add(self.notification_tab, text="Notification API")
        self.tabs.add(self.map_tab, text="Map Preview")
        self.tabs.add(self.profile_tab, text="Result Preview")
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
