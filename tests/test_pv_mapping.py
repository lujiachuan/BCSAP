"""PV 映射配置的校验、持久化与接口行为。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from apps.instrument_service import pv_mapping
from apps.instrument_service.app import create_app
from apps.instrument_service.runtime import InstrumentRuntime
from packages.contracts import PvMappingConfig, PvMappingEntry


def entry(
    signal: str = "quadrupole.q1.current",
    label: str = "Q1 电流",
    pv: str = "BL:Q1:ISET",
    unit: str = "A",
    writable: bool = True,
    required: bool = True,
) -> PvMappingEntry:
    return PvMappingEntry(
        signal=signal,
        label=label,
        pv=pv,
        unit=unit,
        writable=writable,
        required=required,
    )


def config_with(*entries: PvMappingEntry, gateway: str = "simulated") -> PvMappingConfig:
    return PvMappingConfig(
        version=1, gateway=gateway, ca_lib_dir="", entries=list(entries)
    )


def route_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"未找到路由 {method} {path}")


class ValidationTests(unittest.TestCase):
    def test_default_config_has_no_issues(self) -> None:
        self.assertEqual(pv_mapping.validate_config(pv_mapping.default_config()), [])

    def test_duplicate_pv_and_signal_are_reported_per_row(self) -> None:
        config = config_with(
            entry(),
            entry(signal="quadrupole.q2.current", label="Q2 电流", pv="BL:Q1:ISET"),
        )
        issues = pv_mapping.validate_config(config)

        fields = {(issue.index, issue.field) for issue in issues}
        self.assertIn((1, "pv"), fields)
        self.assertNotIn((0, "pv"), fields)
        self.assertIn("重复", next(i.message for i in issues if i.index == 1))

    def test_duplicate_signal_is_reported(self) -> None:
        config = config_with(entry(), entry(pv="BL:Q2:ISET"))
        issues = pv_mapping.validate_config(config)

        self.assertEqual([(i.index, i.field) for i in issues], [(1, "signal")])

    def test_pv_name_with_space_or_bad_signal_is_rejected(self) -> None:
        config = config_with(
            entry(pv="BL:Q1 ISET"),
            entry(signal="BadSignal", label="坏信号", pv="BL:Q2:ISET"),
            entry(signal="", label="空信号", pv="BL:Q3:ISET"),
        )
        issues = pv_mapping.validate_config(config)

        self.assertIn((0, "pv"), {(i.index, i.field) for i in issues})
        self.assertIn((1, "signal"), {(i.index, i.field) for i in issues})
        self.assertIn((2, "signal"), {(i.index, i.field) for i in issues})

    def test_empty_entries_and_blank_label_are_rejected(self) -> None:
        self.assertEqual(
            [(i.index, i.field) for i in pv_mapping.validate_config(config_with())],
            [(-1, "entries")],
        )
        blank = config_with(entry(label="   "))
        self.assertIn((0, "label"), {(i.index, i.field) for i in pv_mapping.validate_config(blank)})

    def test_channel_access_with_missing_ca_dll_is_rejected(self) -> None:
        config = PvMappingConfig(
            version=1,
            gateway="channel-access",
            ca_lib_dir=r"C:\definitely\missing-ca-dir",
            entries=[entry()],
        )
        issues = pv_mapping.validate_config(config)

        self.assertIn((-1, "ca_lib_dir"), {(i.index, i.field) for i in issues})

    def test_pv_pattern_accepts_realistic_epics_names(self) -> None:
        for pv in ("BL:Q1:ISET", "SR:DCCT:Current", "BL:STEER:X", "PV[0]", "A-B_C.1"):
            with self.subTest(pv=pv):
                issues = pv_mapping.validate_config(config_with(entry(pv=pv)))
                self.assertEqual(issues, [])


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("SPECTRUM_PV_MAPPING")
        os.environ["SPECTRUM_PV_MAPPING"] = str(
            Path(self._directory.name) / "pv_mapping.json"
        )

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("SPECTRUM_PV_MAPPING", None)
        else:
            os.environ["SPECTRUM_PV_MAPPING"] = self._previous
        self._directory.cleanup()

    def test_round_trip_preserves_entries(self) -> None:
        config = config_with(
            entry(),
            entry(signal="steerer.x", label="X 偏转", pv="BL:STEER:X", unit="V"),
        )
        path = pv_mapping.save_config(config)

        self.assertTrue(path.is_file())
        self.assertEqual(pv_mapping.load_config(), config)

    def test_missing_file_returns_defaults(self) -> None:
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_corrupted_file_returns_defaults_instead_of_raising(self) -> None:
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text("{ not json", encoding="utf-8")
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_file_with_wrong_shape_returns_defaults(self) -> None:
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text(
            json.dumps({"version": 1, "gateway": "nope", "entries": []}), encoding="utf-8"
        )
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_save_leaves_no_temp_files(self) -> None:
        pv_mapping.save_config(config_with(entry()))
        leftovers = list(Path(self._directory.name).glob(".pv_mapping-*"))
        self.assertEqual(leftovers, [])


class MappingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("SPECTRUM_PV_MAPPING")
        os.environ["SPECTRUM_PV_MAPPING"] = str(
            Path(self._directory.name) / "pv_mapping.json"
        )
        self.app = create_app(InstrumentRuntime(pv_mapping.default_config()))
        self.get_mapping = route_endpoint(self.app, "/control/v1/pv-mapping", "GET")
        self.put_mapping = route_endpoint(self.app, "/control/v1/pv-mapping", "PUT")

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("SPECTRUM_PV_MAPPING", None)
        else:
            os.environ["SPECTRUM_PV_MAPPING"] = self._previous
        self._directory.cleanup()

    def test_put_persists_and_takes_effect_immediately(self) -> None:
        updated = config_with(
            entry(signal="quadrupole.q1.current", label="Q1 电流", pv="SR:Q1:Current")
        )
        saved = self.put_mapping(updated)

        self.assertEqual(saved.entries[0].pv, "SR:Q1:Current")
        self.assertEqual(self.get_mapping().entries[0].pv, "SR:Q1:Current")
        self.assertEqual(pv_mapping.load_config().entries[0].pv, "SR:Q1:Current")

    def test_put_rejects_invalid_config_with_row_level_issues(self) -> None:
        invalid = config_with(entry(pv="BL:Q1 ISET"))

        with self.assertRaises(HTTPException) as caught:
            self.put_mapping(invalid)

        self.assertEqual(caught.exception.status_code, 400)
        issues = caught.exception.detail["issues"]
        self.assertEqual(issues[0]["index"], 0)
        self.assertEqual(issues[0]["field"], "pv")
        # 校验失败不得写盘
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_added_row_becomes_visible_in_health_check(self) -> None:
        added = config_with(
            entry(
                signal="quadrupole.q3.current",
                label="Q3 电流",
                pv="BL:Q3:ISET",
                unit="A",
                required=False,
            )
        )
        self.put_mapping(added)

        health = route_endpoint(self.app, "/control/v1/pvs/health", "GET")()
        self.assertEqual(health.summary.total, 1)
        self.assertEqual(health.items[0].signal, "quadrupole.q3.current")
        self.assertEqual(health.items[0].pv, "BL:Q3:ISET")

    def test_deleted_row_disappears_from_health_check(self) -> None:
        trimmed = config_with(
            entry(),
            entry(signal="steerer.x", label="X 偏转", pv="BL:STEER:X", unit="V"),
        )
        self.put_mapping(trimmed)

        health = route_endpoint(self.app, "/control/v1/pvs/health", "GET")()
        self.assertEqual(
            [item.signal for item in health.items],
            ["quadrupole.q1.current", "steerer.x"],
        )


if __name__ == "__main__":
    unittest.main()
