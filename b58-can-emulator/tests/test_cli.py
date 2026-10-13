import contextlib
import io
import unittest
from unittest import mock

from bmw_b58_emu import __main__ as cli
from bmw_b58_emu import tester


class CliTests(unittest.TestCase):
    def run_quiet(self, fn, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                rc = fn(argv)
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def test_missing_interface_is_a_clean_error(self):
        rc, err = self.run_quiet(cli.main, ["-i", "nosuchcan9", "--api-port", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot open CAN interface 'nosuchcan9'", err)
        self.assertIn("setup_vcan.sh", err)

    def test_create_vcan_failure_is_reported(self):
        with mock.patch.object(cli, "ensure_vcan", side_effect=RuntimeError("no permission")):
            rc, err = self.run_quiet(cli.main, ["-i", "vcanX", "--create-vcan", "--api-port", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("no permission", err)

    def test_vin_is_validated(self):
        self.assertEqual(cli._vin(" wba5b58000fa00001 "), "WBA5B58000FA00001")
        for bad in ("SHORT", "WBA5B58000FA0000I", "WBA5B58000FA0000-", "WBA5B58000FA000012"):
            rc, err = self.run_quiet(cli.main, ["--vin", bad])
            self.assertEqual(rc, 2, bad)
            self.assertIn("VIN must be", err)

    def test_bad_choice_rejected(self):
        rc, _ = self.run_quiet(cli.main, ["--scenario", "teleport"])
        self.assertEqual(rc, 2)

    def test_tester_errors(self):
        rc, err = self.run_quiet(tester.main, ["-i", "nosuchcan9", "obd", "01", "0C"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot open CAN interface", err)
        rc, err = self.run_quiet(tester.main, ["obd", "zz"])
        self.assertEqual(rc, 2)
        self.assertIn("must be hex", err)
