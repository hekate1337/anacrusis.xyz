import json
import threading
import urllib.error
import urllib.request

from bmw_b58_emu.api import make_server
from .helpers import EmuCase


class ApiTests(EmuCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.dme, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_drive_and_inject_via_api_visible_on_can(self):
        self.assertEqual(self.call("POST", "/api/engine", {"action": "start"})[0], 200)
        self.wait(lambda: self.vehicle.engine == 2, 5)
        self.assertEqual(self.call("POST", "/api/driver", {"mode": "cruise", "target_speed": 60})[0], 200)
        self.assertEqual(self.call("POST", "/api/dtcs", {"code": "P0300"})[0], 200)
        self.assertEqual(self.call("POST", "/api/override", {"signal": "coolant", "value": 111})[0], 200)
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["driver"]["mode"], "cruise")
        r = self.obd.request(bytes([1, 0x05]))
        self.assertEqual(r[2] - 40, 111)
        self.assertEqual(self.obd.request(bytes([3]))[:4], bytes([0x43, 1, 0x03, 0x00]))
        self.assertEqual(self.call("GET", "/api/dtcs")[1][0]["code"], "P0300")
        self.call("DELETE", "/api/override")
        self.call("DELETE", "/api/dtcs")
        self.assertEqual(self.obd.request(bytes([3])), bytes([0x43, 0]))

    def test_validation(self):
        self.assertEqual(self.call("POST", "/api/dtcs", {"code": "XYZ"})[0], 400)
        self.assertEqual(self.call("POST", "/api/override", {"signal": "nope", "value": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/driver", {"mode": "teleport"})[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
