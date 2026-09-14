"""调束可选项目录与束线拓扑：谁能当目标、谁能当变量、上游关系怎么算。

这些断言的落点是**安全语义**而不是界面长相：把保护参数（磁铁变化速率）或非束流
读数放进优化列表，优化器会朝没有物理意义的方向跑，而且不会报错。
"""

from __future__ import annotations

import unittest

from apps.instrument_service import pv_mapping
from apps.instrument_service.app import create_app
from apps.instrument_service.tuning_catalog import (
    REASON_NOT_BEAM,
    REASON_NOT_DECLARED,
    REASON_RATE,
    build_catalog,
)
from packages.contracts import PvMappingConfig, PvMappingEntry
from packages.domain import beamline


def entry(signal: str, group: str = "磁铁电源", **overrides: object) -> PvMappingEntry:
    base = {
        "signal": signal,
        "label": signal,
        "pv": "PV:" + signal,
        "unit": "",
        "writable": True,
        "required": False,
        "group": group,
        "role": "setpoint",
    }
    base.update(overrides)
    return PvMappingEntry(**base)  # type: ignore[arg-type]


class BeamlineTopologyTests(unittest.TestCase):
    """拓扑是工程假设，但假设本身必须自洽：顺序、上游、未知组的行为。"""

    def test_stages_cover_the_detector_group_and_are_ordered(self) -> None:
        groups = [group for stage in beamline.STAGES for group in stage.groups]
        self.assertEqual(groups[-1], beamline.DETECTOR_GROUP)
        self.assertEqual(len(groups), len(set(groups)), "同一个组不能出现在两段里")

    def test_everything_is_upstream_of_the_detector(self) -> None:
        upstream = beamline.upstream_groups_of(beamline.DETECTOR_GROUP)
        self.assertIn("磁铁电源", upstream)
        self.assertIn("气体流量", upstream)
        self.assertNotIn(beamline.DETECTOR_GROUP, upstream, "探测器自己不是它的上游")

    def test_magnet_group_has_the_upstream_stages_before_it(self) -> None:
        upstream = beamline.upstream_groups_of("磁铁电源")
        self.assertIn("气体流量", upstream)
        self.assertNotIn("束流探测", upstream)
        self.assertFalse(beamline.is_upstream_of("束流探测", "磁铁电源"))

    def test_unknown_group_is_not_silently_treated_as_upstream(self) -> None:
        """没登记过的自定义组要显式返回空——"不知道在哪一段"不能假装知道。"""
        self.assertEqual(beamline.upstream_groups_of("自建组"), ())
        self.assertEqual(beamline.stage_key_of_group("自建组"), "")


class TuningCatalogTests(unittest.TestCase):
    def test_real_mapping_targets_are_beam_detectors_only(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())

        self.assertEqual(
            [target.signal for target in catalog.targets],
            ["detector.fc1.beam_current", "detector.fc2.beam_current"],
        )

    def test_real_mapping_variables_exclude_rate_and_switches(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())
        signals = [variable.signal for variable in catalog.variables]

        self.assertIn("magnet.m1.current_setpoint", signals)
        self.assertIn("hv_array.dw04.voltage_setpoint", signals)
        self.assertFalse([s for s in signals if s.endswith("current_rate_setpoint")])
        self.assertFalse([s for s in signals if s.endswith(".switch")])
        self.assertAlmostEqual(len(signals), len(set(signals)), places=0)

    def test_rate_setpoints_are_excluded_with_a_stated_reason(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())

        self.assertEqual(
            sorted(catalog.excluded.get(REASON_RATE, [])),
            [f"magnet.m{n}.current_rate_setpoint" for n in (1, 2, 3, 4)],
        )
        self.assertNotIn("magnet.m1.current_rate_setpoint", [
            variable.signal for variable in catalog.variables
        ])

    def test_readbacks_are_excluded_from_targets_with_a_stated_reason(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())
        excluded = catalog.excluded.get(REASON_NOT_BEAM, [])

        self.assertIn("vacuum.chamber_pressure", excluded)
        self.assertIn("hv_array.dw04.voltage_readback", excluded)
        self.assertNotIn("detector.fc1.beam_current", excluded)

    def test_upstream_of_a_target_lists_only_declared_variables(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())
        upstream = catalog.upstream["detector.fc1.beam_current"]

        self.assertEqual(
            sorted(upstream), sorted(v.signal for v in catalog.variables)
        )
        self.assertNotIn("magnet.m1.current_rate_setpoint", upstream)

    def test_flags_default_to_not_participating(self) -> None:
        """没标记的映射什么都不给：默认「不参与」而不是「能写就算可调」。"""
        config = PvMappingConfig(
            version=1,
            entries=[entry("magnet.m1.current_setpoint"), entry("x.read", writable=False,
                                                                role="readback")],
        )

        catalog = build_catalog(config)

        self.assertEqual(catalog.variables, [])
        self.assertEqual(catalog.targets, [])
        self.assertIn("magnet.m1.current_setpoint", catalog.excluded[REASON_NOT_DECLARED])

    def test_declared_variables_and_targets_are_listed(self) -> None:
        config = PvMappingConfig(
            version=1,
            entries=[
                entry("magnet.m1.current_setpoint", tunable=True, min_value=0.0,
                      max_value=600.0, max_step=100.0),
                entry("detector.fc1.beam_current", group="束流探测", writable=False,
                      role="readback", beam_target=True),
            ],
        )

        catalog = build_catalog(config)

        self.assertEqual([v.signal for v in catalog.variables],
                         ["magnet.m1.current_setpoint"])
        self.assertEqual(catalog.variables[0].max_step, 100.0)
        self.assertEqual([t.signal for t in catalog.targets],
                         ["detector.fc1.beam_current"])
        self.assertEqual(catalog.targets[0].stage, "detector")
        self.assertEqual(catalog.variables[0].stage, "magnet")

    def test_rate_setpoint_is_never_a_variable_even_if_flagged(self) -> None:
        """手改映射把速率标成可调也不行：保护参数不进搜索空间。"""
        config = PvMappingConfig(
            version=1,
            entries=[entry("magnet.m1.current_rate_setpoint", tunable=True)],
        )

        catalog = build_catalog(config)

        self.assertEqual(catalog.variables, [])
        self.assertIn("magnet.m1.current_rate_setpoint", catalog.excluded[REASON_RATE])

    def test_linked_sets_are_reported_as_information(self) -> None:
        catalog = build_catalog(pv_mapping.default_config())

        self.assertIn(
            ["magnet.m1.current_setpoint", "magnet.m2.current_setpoint"],
            catalog.linked_sets,
        )

    def test_endpoint_serves_the_catalog(self) -> None:
        from apps.instrument_service.runtime import InstrumentRuntime

        runtime = InstrumentRuntime(
            pv_mapping.default_config(),
            gateway_factory=lambda config: None,  # 目录不碰硬件
        )
        try:
            app = create_app(runtime)
            route = next(
                r for r in app.routes
                if getattr(r, "path", None) == "/control/v1/tuning/catalog"
            )
            catalog = route.endpoint()
            self.assertTrue(catalog.targets)
            self.assertTrue(catalog.stages)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
