import contextlib, importlib.util, io, json, pathlib, sys, unittest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import mock_cloudstack_api as mock

spec_ = importlib.util.spec_from_file_location("csb", ROOT / "master/cs-bootstrap.py")
csb = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(csb)


def spec(port):
    s = json.loads((ROOT / "docs/cs-zone-spec.example.json").read_text())
    s.update(mgmt_url=f"http://127.0.0.1:{port}", kvm_hosts=[{"ip": "10.0.0.41", "user": "root", "password": "Sup3rS3cret!"},
                                                           {"ip": "10.0.0.42", "user": "root", "password": "Sup3rS3cret!"}])
    return s


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.S = mock.State(); self.srv = mock.make_server(self.S); self.port = self.srv.server_address[1]

    def tearDown(self): self.srv.shutdown()

    def run_main(self, s):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): csb.main(s)
        return buf.getvalue()

    def test_full_run_and_idempotency(self):
        s = spec(self.port); out = self.run_main(s); S = self.S
        self.assertEqual(S.bad_signatures, 0)
        z = list(S.zones.values()); self.assertEqual(len(z), 1); self.assertEqual(z[0]["networktype"], "Advanced"); self.assertEqual(z[0]["allocationstate"], "Enabled")
        pn = list(S.pns.values()); self.assertEqual((len(pn), pn[0]["state"]), (1, "Enabled"))
        self.assertEqual(sorted(S.traffic[pn[0]["id"]]), ["Guest", "Management", "Public"])
        self.assertTrue(all(p["state"] == "Enabled" for p in S.providers.values()) and len(S.providers) == 3)
        self.assertEqual((len(S.pods), len(S.vlans), len(S.clusters), len(S.hosts), len(S.pools), len(S.stores)), (1, 1, 1, 2, 1, 1))
        self.assertEqual(S.config["host"], s["management_vip"])
        self.assertGreater(S.polls, 0, "async jobs must be polled")
        self.assertNotIn("Sup3rS3cret!", out); self.assertEqual(S.host_passwords, ["Sup3rS3cret!"] * 2)
        creates = [c for c in S.calls if c.startswith(("create", "add"))]
        out2 = self.run_main(s)                                                      # second run changes nothing
        self.assertEqual([c for c in S.calls if c.startswith(("create", "add"))], creates)
        self.assertEqual((len(S.zones), len(S.hosts), len(S.pools), len(S.stores)), (1, 2, 1, 1))
        self.assertIn("exists", out2)

    def test_wrong_admin_password(self):
        s = spec(self.port); s["admin_password"] = "nope"
        with self.assertRaises(SystemExit): self.run_main(s)

    def test_tampered_secret_is_rejected(self):
        cs = csb.CS(f"http://127.0.0.1:{self.port}"); cs.login("admin", "password"); cs.secret = "wrong"
        with self.assertRaises(SystemExit): cs.run("listZones")
        self.assertEqual(self.S.bad_signatures, 1)

    def test_missing_parameter_is_reported(self):
        cs = csb.CS(f"http://127.0.0.1:{self.port}"); cs.login("admin", "password")
        with self.assertRaises(SystemExit) as e: cs.run("createZone", name="x")
        self.assertIn("missing parameter", str(e.exception))


if __name__ == "__main__":
    unittest.main()
