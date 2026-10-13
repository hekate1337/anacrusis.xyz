"""HTTP/JSON control plane (stdlib only) for driving the simulation and injecting faults."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import dtc as dtcmod
from .vehicle import SIGNALS, Ignition

if TYPE_CHECKING:  # pragma: no cover
    from .dme import Dme

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, dme: "Dme") -> None:
        self.dme = dme

    # each handler returns a JSON-serialisable value
    def state(self, body: Any, q: Dict) -> Any:
        d = self.dme
        snap = d.vehicle.snapshot()
        return {
            "signals": snap,
            "driver": {"mode": d.vehicle.driver.mode, "target_speed": d.vehicle.driver.target_speed},
            "overrides": dict(d.vehicle.overrides),
            "dme": {
                "address": d.config.address,
                "vin": d.config.vin,
                "session": d.uds.session,
                "security_level": d.uds.security_level,
                "comm_tx_enabled": d.comm_tx_enabled,
                "dtc_logging": d.dtcs.logging_enabled,
                "online": d.online(),
                "frames_tx": d.node.tx_count,
                "frames_rx": d.node.rx_count,
            },
        }

    def dtcs_get(self, body: Any, q: Dict) -> Any:
        return [
            {"code": e.code, "status": f"0x{e.status:02X}", "description": e.description, "occurrences": e.occurrences, "has_freeze_frame": bool(e.freeze)}
            for e in self.dme.dtcs.entries()
        ]

    def dtcs_post(self, body: Any, q: Dict) -> Any:
        code = (body or {}).get("code")
        if not code:
            raise ApiError(400, "body needs {'code': 'P0301'}")
        try:
            e = self.dme.set_fault(code, bool(body.get("confirmed", True)))
        except ValueError as ex:
            raise ApiError(400, str(ex))
        if e is None:
            raise ApiError(409, "DTC logging is disabled (UDS 0x85 02 active)")
        return {"code": e.code, "status": f"0x{e.status:02X}"}

    def dtcs_delete(self, body: Any, q: Dict) -> Any:
        code = (q.get("code") or [None])[0]
        self.dme.clear_dtcs(code)
        return {"cleared": code or "all"}

    def ignition(self, body: Any, q: Dict) -> Any:
        names = {"off": Ignition.OFF, "accessory": Ignition.ACCESSORY, "on": Ignition.ON}
        state = (body or {}).get("state")
        if state not in names:
            raise ApiError(400, f"state must be one of {list(names)}")
        self.dme.vehicle.set_ignition(names[state])
        return {"ignition": state}

    def engine(self, body: Any, q: Dict) -> Any:
        action = (body or {}).get("action")
        if action == "start":
            self.dme.vehicle.start_engine()
        elif action == "stop":
            self.dme.vehicle.stop_engine()
        else:
            raise ApiError(400, "action must be 'start' or 'stop'")
        return {"engine": action}

    def driver(self, body: Any, q: Dict) -> Any:
        body = body or {}
        veh = self.dme.vehicle
        try:
            veh.driver.configure(body.get("mode", veh.driver.mode), body.get("target_speed"))
            if "pedal" in body:
                veh.pedal = max(0.0, min(100.0, float(body["pedal"])))
            if "brake" in body:
                veh.brake = max(0.0, min(100.0, float(body["brake"])))
        except (ValueError, TypeError) as ex:
            raise ApiError(400, str(ex))
        return {"mode": veh.driver.mode, "target_speed": veh.driver.target_speed}

    def override_post(self, body: Any, q: Dict) -> Any:
        try:
            self.dme.vehicle.set_override((body or {}).get("signal", ""), (body or {}).get("value"))
        except (ValueError, TypeError) as ex:
            raise ApiError(400, str(ex))
        return {"overrides": dict(self.dme.vehicle.overrides)}

    def override_delete(self, body: Any, q: Dict) -> Any:
        self.dme.vehicle.clear_override((q.get("signal") or [None])[0])
        return {"overrides": dict(self.dme.vehicle.overrides)}

    def index(self, body: Any, q: Dict) -> Any:
        return {"routes": sorted(f"{m} {p}" for (m, p) in self.routes()), "signals": list(SIGNALS)}

    def routes(self) -> Dict[Tuple[str, str], Any]:
        return {
            ("GET", "/"): self.index,
            ("GET", "/api/state"): self.state,
            ("GET", "/api/dtcs"): self.dtcs_get,
            ("POST", "/api/dtcs"): self.dtcs_post,
            ("DELETE", "/api/dtcs"): self.dtcs_delete,
            ("POST", "/api/ignition"): self.ignition,
            ("POST", "/api/engine"): self.engine,
            ("POST", "/api/driver"): self.driver,
            ("POST", "/api/override"): self.override_post,
            ("DELETE", "/api/override"): self.override_delete,
        }


def make_server(dme: "Dme", host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    api = Api(dme)
    routes = api.routes()

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            fn = routes.get((method, url.path.rstrip("/") or "/"))
            try:
                if fn is None:
                    raise ApiError(404, "not found")
                body = None
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(min(length, 65536)))
                    except ValueError:
                        raise ApiError(400, "invalid JSON body")
                    if not isinstance(body, dict):
                        raise ApiError(400, "JSON body must be an object")
                status, out = 200, fn(body, parse_qs(url.query))
            except ApiError as e:
                status, out = e.status, {"error": str(e)}
            except Exception as e:  # pragma: no cover
                log.exception("API error")
                status, out = 500, {"error": str(e)}
            raw = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("api: " + fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)
