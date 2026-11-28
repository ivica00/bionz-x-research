"""Tests for one-click USB debugger setup and onboarding bundle tooling."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from src.a6400.targets.usb_debugger_compat import (
    PAYLOAD_ASSETS,
    find_debugger_target_by_model,
    get_debugger_target,
)
from tools.research.onboard_debugger_bundle import inspect_and_unpack_bundle
from tools.setup_usb_debugger import (
    build_onboarding_bundle,
    detect_msc_camera,
    ensure_payload_binaries,
)

ROOT = Path(__file__).resolve().parents[1]
ILCE_7C_PULL = ROOT / "artifacts/hardware/ilce-7c-2.00-pull"


class _FakeSenserCam:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.writes: list[tuple[str, bytes]] = []

    def readFile(self, path: str) -> bytes:
        if path not in self.files:
            raise RuntimeError("Senser file control error 130")
        return self.files[path]

    def writeFile(self, path: str, data: bytes) -> None:
        self.writes.append((path, data))
        self.files[path] = data


class TestSetupUsbDebugger(unittest.TestCase):
    def test_windows_mass_storage_survives_libusb_open_failure(self):
        device_type = type("SonyExtCmdDevice", (), {})
        native_camera = device_type()
        import_driver = mock.Mock(side_effect=nullcontext)

        def list_devices(driver, quiet=False):
            yield native_camera
            if driver is None:
                raise NotImplementedError("Operation not supported or unimplemented on this platform")

        camera = mock.Mock()
        camera.getCameraInfo.return_value = SimpleNamespace(modelName="ILCE-7M3", modelCode="12345")
        updater = mock.Mock()
        updater.getFirmwareVersion.return_value = ("4.04", "0.00")
        modules = {
            "pmca.commands.usb": SimpleNamespace(importDriver=import_driver, listDevices=list_devices),
            "pmca.usb.sony": SimpleNamespace(
                SonyExtCmdDevice=device_type, SonyExtCmdCamera=lambda _: camera,
                SonyUpdaterCamera=lambda _: updater,
            ),
        }
        with mock.patch.dict("sys.modules", modules), mock.patch("tools.camera_debugger.sys.platform", "win32"):
            identity = detect_msc_camera()

        self.assertEqual(identity, {"model_name": "ILCE-7M3", "model_code": "12345", "firmware_version": "4.04"})
        self.assertEqual(import_driver.call_args_list, [mock.call(None), mock.call("native")])
        camera.getCameraInfo.assert_called_once_with()
        updater.getFirmwareVersion.assert_called_once_with()

    def test_find_debugger_target_by_model(self):
        self.assertEqual(find_debugger_target_by_model("ilce-7c", "2.00").target_id, "ilce_7c")
        self.assertEqual(find_debugger_target_by_model("ILCE-6400", "2.00").target_id, "ilce_6400")
        self.assertIsNone(find_debugger_target_by_model("ILCE-9M2", "2.00"))

    def test_ensure_payload_binaries_downloads_and_verifies_sha256(self):
        spec = get_debugger_target("ilce_7c")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            fake_files: dict[str, bytes] = {}
            for rel, meta in PAYLOAD_ASSETS.items():
                src = ROOT / rel
                if not src.is_file():
                    self.skipTest(f"Prebuilt payload missing locally: {rel}")
                fake_files[str(meta["url"])] = src.read_bytes()

            resolved = ensure_payload_binaries(
                spec,
                root=tmp_root,
                fetcher=lambda url: fake_files[url],
            )
            self.assertTrue(resolved[spec.payload_daemon].is_file())
            self.assertTrue(resolved[spec.payload_launcher].is_file())

            # Corrupt one payload -> must raise RuntimeError
            resolved[spec.payload_daemon].write_bytes(b"corrupted")
            with self.assertRaises(RuntimeError):
                ensure_payload_binaries(spec, root=tmp_root, fetcher=lambda url: fake_files[url])

    @unittest.skipUnless(
        (ILCE_7C_PULL / "usb_extcmd.ko").is_file()
        and (ILCE_7C_PULL / "libusbcmd.so").is_file(),
        "ILCE-7C 2.00 pull fixtures not present",
    )
    def test_onboarding_bundle_and_developer_unpack(self):
        actual_ko = (ILCE_7C_PULL / "usb_extcmd.ko").read_bytes()
        actual_lib = (ILCE_7C_PULL / "libusbcmd.so").read_bytes()
        cam = _FakeSenserCam(
            {
                "/lib/libc.so.6": b"FAKE_LIBC",
                "/proc/version": b"Linux (none) 3.0.27_nl-rt106+\n",
                "/proc/cpuinfo": b"Processor : ARMv7 Processor\n",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            zip_path = build_onboarding_bundle(
                cam,
                {"model_name": "ILCE-9999", "model_code": "0000000000", "firmware_version": "1.00"},
                actual_ko,
                actual_lib,
                b"",
                False,
                root=tmp_root,
            )
            self.assertEqual(cam.writes, [], "Onboarding must never write to the camera")
            self.assertTrue(zip_path.is_file())

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("camera_identity.json", names)
                self.assertIn("compatibility_report.json", names)
                self.assertIn("usb_extcmd.ko", names)
                self.assertIn("libusbcmd.so", names)
                self.assertIn("libc.so.6", names)
                self.assertIn("proc/version.txt", names)
                report = json.loads(zf.read("compatibility_report.json").decode("utf-8"))
                self.assertTrue(report["usb_extcmd"]["matches_verified_baseline_normalized"])
                self.assertTrue(report["libusbcmd"]["matches_verified_baseline"])

            unpacked = inspect_and_unpack_bundle(zip_path, root=tmp_root)
            self.assertTrue(unpacked["baseline_compatible"])
            self.assertIn('target_id="ilce_9999"', unpacked["spec_snippet"])

    def test_run_setup_fails_cleanly_when_senser_times_out(self):
        import io
        import types
        from contextlib import redirect_stdout
        from tools import setup_usb_debugger

        fake_usb_mod = types.SimpleNamespace(senserShellCommand=lambda **kw: None)
        fake_pmca = types.SimpleNamespace(commands=types.SimpleNamespace(usb=fake_usb_mod))
        buf = io.StringIO()
        with (
            mock.patch.object(setup_usb_debugger, "ensure_pmca_on_path", return_value=Path(".")),
            mock.patch.object(
                setup_usb_debugger,
                "detect_msc_camera",
                return_value={"model_name": "ILCE-6400", "model_code": "0032850340", "firmware_version": "2.00"},
            ),
            mock.patch.object(setup_usb_debugger, "ensure_payload_binaries", return_value={}),
            mock.patch.dict("sys.modules", {"pmca": fake_pmca, "pmca.commands": fake_pmca.commands, "pmca.commands.usb": fake_usb_mod}),
            redirect_stdout(buf),
        ):
            rc = setup_usb_debugger.run_setup(skip_post_boot_test=True)
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("[ERROR] Senser service-mode session did not complete", out)
        self.assertNotIn("Deployed and verified!", out)


if __name__ == "__main__":
    unittest.main()
