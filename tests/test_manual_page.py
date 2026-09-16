"""手动控制测试页：三列分列、设备折行、滚轮与步进档、开关按钮同步、全部关断。

这些用例不连执行服务：页面装配与批量关断逻辑都是纯本地的，构造行控件用
直接的配置字典，避免测试依赖网络。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel

from apps.desktop_client import instrument_api
from apps.desktop_client.pages import manual
from apps.desktop_client.pages.registry import page_specs


class _GroupPanelSignal:
    """成组下发请求线程的 completed 信号替身。"""

    def __init__(self, payload: dict | None) -> None:
        self._payload = payload

    def connect(self, callback, *_args, **_kwargs) -> None:
        if self._payload is not None:
            callback(self._payload)


class _GroupPanelThread:
    """替掉成组下发的请求线程：单测不连服务，也不留 QThread。"""

    def __init__(self, payload: dict | None = None) -> None:
        self.completed = _GroupPanelSignal(payload)


def entry(signal: str, **overrides: object) -> dict:
    base = {
        "signal": signal,
        "label": signal,
        "pv": "PV:" + signal,
        "unit": "",
        "writable": False,
        "required": False,
        "group": "测试组",
        "readback_signal": "",
        "role": "readback",
        "min_value": None,
        "max_value": None,
        "max_step": None,
        "max_rate": None,
        "settle_tol": None,
        "settle_timeout": None,
    }
    base.update(overrides)
    return base


def connected(value: float) -> dict:
    return {"connected": True, "value": value}


def offline() -> dict:
    return {"connected": False, "value": None}


def wheel(spin, ticks: int) -> None:
    """往设定框上滚一格（角增量 120 为一格）。"""
    event = QWheelEvent(
        QPointF(5.0, 5.0),
        QPointF(5.0, 5.0),
        QPoint(0, 0),
        QPoint(0, 120 * ticks),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    spin.wheelEvent(event)


DW_SETPOINT = "hv_array.dw04.voltage_setpoint"
DW_SWITCH = "hv_array.dw04.switch"
DW_VOLTAGE = "hv_array.dw04.voltage_readback"
DW_CURRENT = "hv_array.dw04.current_readback"
DW_SIGNALS = (DW_SETPOINT, DW_SWITCH, DW_VOLTAGE, DW_CURRENT)


def dw_entries(group: str = "高压阵列 DW") -> list[dict]:
    """一路 DW 高压电源：电压设定 + 输出使能 + 电压/电流回读。"""
    return [
        entry(DW_SETPOINT, label="DW4 通道4 电压设定", writable=True, role="setpoint",
              unit="V", min_value=0.0, max_value=5100.0, group=group,
              readback_signal=DW_VOLTAGE),
        entry(DW_SWITCH, label="DW4 通道4 输出使能", writable=True, role="toggle",
              min_value=0.0, max_value=1.0, group=group),
        entry(DW_VOLTAGE, label="DW4 通道4 电压回读", unit="V", group=group),
        entry(DW_CURRENT, label="DW4 通道4 电流回读", unit="mA", group=group),
    ]


def dw_readings(
    voltage: float = 2000.0, current: float = 1.5, switch: float = 1.0
) -> dict:
    return {
        DW_SETPOINT: connected(voltage),
        DW_SWITCH: connected(switch),
        DW_VOLTAGE: connected(voltage),
        DW_CURRENT: connected(current),
    }


class StepSizeTests(unittest.TestCase):
    """步进幅度按量程推导，避免出现「0-50 kV 的主高压 ±10」这种没用的按钮。"""

    def test_floor_nice_snaps_to_1_2_5(self) -> None:
        for value, expected in (
            (10.0, 10.0),
            (12.0, 10.0),
            (102.0, 100.0),
            (0.2, 0.2),
            (1.0, 1.0),
            (49.0, 20.0),
        ):
            with self.subTest(value=value):
                self.assertEqual(manual._floor_nice(value), expected)

    def test_floor_nice_handles_degenerate_input(self) -> None:
        for value in (0.0, -1.0, float("nan")):
            self.assertEqual(manual._floor_nice(value), 1.0)

    def test_step_sizes_are_a_fixed_fraction_of_the_span(self) -> None:
        """0-500 sccm → 基准档 1、粗档 10，即量程的 0.2% / 2%。"""
        fine, coarse = manual._step_sizes(entry("g", min_value=0.0, max_value=500.0))

        self.assertEqual((fine, coarse), (1.0, 10.0))

    def test_step_sizes_for_main_high_voltage_are_sub_unit(self) -> None:
        fine, coarse = manual._step_sizes(entry("h", min_value=0.0, max_value=50.0))

        self.assertEqual((fine, coarse), (0.1, 1.0))

    def test_step_sizes_fall_back_without_bounds(self) -> None:
        self.assertEqual(manual._step_sizes(entry("x")), (1.0, 10.0))

    def test_decimals_follow_step_magnitude(self) -> None:
        self.assertEqual(manual._decimals_for(10.0), 0)
        self.assertEqual(manual._decimals_for(0.2), 1)
        self.assertEqual(manual._decimals_for(0.01), 2)


class InputFormatTests(unittest.TestCase):
    """设定框格式：设定框是唯一输入口，小数位必须够用。"""

    def test_one_more_decimal_than_the_step(self) -> None:
        self.assertEqual(
            manual._input_format(entry("g", min_value=0.0, max_value=500.0)), (1.0, 1)
        )

    def test_sub_unit_range_still_allows_a_decimal(self) -> None:
        """0-50 kV 若只给 0 位小数，就再也输不进 12.5。"""
        _, decimals = manual._input_format(entry("h", min_value=0.0, max_value=50.0))

        self.assertEqual(decimals, 2)

    def test_decimals_are_capped(self) -> None:
        _, decimals = manual._input_format(entry("x", min_value=0.0, max_value=0.001))

        self.assertEqual(decimals, 3)


def real_setpoints() -> list[dict]:
    """现场映射里所有可写设定量（不在测试里另抄一份量程）。"""
    from apps.instrument_service.pv_mapping import default_config

    return [
        item.model_dump()
        for item in default_config().entries
        if item.role == "setpoint"
    ]


def wheel_delta(setpoint: dict, level: int, start: float) -> float:
    """从 start 起滚一格，返回**设定框真正接受**的增量。

    走真实 `_WheelSpinBox.wheelEvent`，而不是在测试里复算一遍算式：
    增量与步长不一致（被小数位或量程端点夹掉）正是这里要抓的问题。
    """
    base, decimals = manual._input_format(setpoint)
    spin = manual._WheelSpinBox()
    spin.setDecimals(decimals)
    spin.setRange(float(setpoint["min_value"]), float(setpoint["max_value"]))
    spin.setSingleStep(base)
    spin.setValue(start)
    spin.setSingleStep(base * level)
    before = spin.value()
    wheel(spin, 1)
    return spin.value() - before


class StepLevelRatioTests(unittest.TestCase):
    """×1 / ×10 / ×100 三档必须真的按 10 倍递进。

    现场反馈「比例不对，不是按实际 × 的比例滚动变化」：旧基准步长取量程的 1/50，
    ×100 就是量程的 200%，一格必然顶到端点被夹住——DW 通道设在 5000 V 时三档
    增量全是 +100 V，看上去 ×1/×10/×100 完全一样。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_levels_are_exactly_ten_times_apart(self) -> None:
        setpoints = real_setpoints()
        self.assertTrue(setpoints, "现场映射里应有可写设定量")

        for setpoint in setpoints:
            base, _decimals = manual._input_format(setpoint)
            with self.subTest(signal=setpoint["signal"]):
                steps = [base * level for level in manual.STEP_LEVELS]
                self.assertEqual(steps[1], steps[0] * 10)
                self.assertEqual(steps[2], steps[1] * 10)

    def test_every_level_moves_by_its_step_from_mid_range(self) -> None:
        """量程中点起滚一格，三档的增量都必须等于各自步长（没被夹住）。"""
        for setpoint in real_setpoints():
            base, _decimals = manual._input_format(setpoint)
            low = float(setpoint["min_value"])
            high = float(setpoint["max_value"])
            mid = low + (high - low) / 2.0
            for level in manual.STEP_LEVELS:
                with self.subTest(signal=setpoint["signal"], level=level):
                    self.assertAlmostEqual(
                        wheel_delta(setpoint, level, mid), base * level
                    )

    def test_coarse_level_stays_inside_the_span(self) -> None:
        """最粗一档不超过量程的 25%，否则一滚就到端点、和 ×10 没区别。"""
        for setpoint in real_setpoints():
            base, _decimals = manual._input_format(setpoint)
            span = float(setpoint["max_value"]) - float(setpoint["min_value"])
            with self.subTest(signal=setpoint["signal"]):
                self.assertLessEqual(base * manual.STEP_LEVELS[-1], span * 0.25)

    def dw_setpoint(self) -> dict:
        return entry(
            DW_SETPOINT, writable=True, role="setpoint",
            min_value=0.0, max_value=5100.0,
        )

    def test_levels_differ_at_a_live_operating_point(self) -> None:
        """回归：DW 通道 5100 V 量程上，3000 V 处三档应是 10 / 100 / 1000 V。

        旧基准步长（量程的 1/50 = 100 V）在这里 ×100 也只能 +2100 V——被端点夹住，
        和 ×10 看不出差别，这就是现场看到的「比例不对」。
        """
        setpoint = self.dw_setpoint()

        self.assertEqual(wheel_delta(setpoint, 1, 3000.0), 10.0)
        self.assertEqual(wheel_delta(setpoint, 10, 3000.0), 100.0)
        self.assertEqual(wheel_delta(setpoint, 100, 3000.0), 1000.0)

    def test_step_is_capped_only_by_the_remaining_headroom(self) -> None:
        """靠近端点时增量只能给到剩余余量——这是量程决定的，不是档位失效。

        5000 V 处距上限只剩 100 V，所以任何不小于 100 V 的档位都只能 +100 V；
        量程内其它位置（见上一个用例）三档严格成 10 倍。
        """
        setpoint = self.dw_setpoint()

        self.assertEqual(wheel_delta(setpoint, 10, 5000.0), 100.0)
        self.assertEqual(wheel_delta(setpoint, 100, 5000.0), 100.0)


class DeviceMergeTests(unittest.TestCase):
    """设备折行与设备名推导。"""

    def test_device_name_is_the_signal_without_its_last_segment(self) -> None:
        self.assertEqual(manual._device_of(DW_SETPOINT), "hv_array.dw04")

    def test_device_name_falls_back_to_the_signal_itself(self) -> None:
        self.assertEqual(manual._device_of("plain"), "plain")

    def test_namespace_is_the_first_segment(self) -> None:
        self.assertEqual(manual._namespace_of("hv_bd.cylinder1"), "hv_bd")
        self.assertEqual(manual._namespace_of("vacuum"), "vacuum")

    def test_common_label_is_the_shared_prefix(self) -> None:
        self.assertEqual(
            manual._common_label(
                ["DW4 通道4 电压设定", "DW4 通道4 输出使能", "DW4 通道4 电压回读"]
            ),
            "DW4 通道4",
        )

    def test_common_label_ignores_an_outlier_label(self) -> None:
        """「灭弧」和设备名没有共同前缀，不该把设备名拉空。"""
        self.assertEqual(
            manual._common_label(["溅射功率设定", "溅射电源使能", "灭弧", "溅射功率回读"]),
            "溅射",
        )

    def test_common_label_of_a_single_label_is_that_label(self) -> None:
        self.assertEqual(manual._common_label(["FC1 束流电流"]), "FC1 束流电流")

    def test_common_label_backs_off_to_a_word_boundary(self) -> None:
        """设备只有电压类信号时前缀会停在「A 电压」——必须退到「A」，
        否则「电压设定 / 电流设定」两个量名会双双被吃成「设定」。"""
        self.assertEqual(manual._common_label(["A 电压设定", "A 电压回读"]), "A")

    def test_short_label_strips_the_device_name(self) -> None:
        self.assertEqual(
            manual._short_label("DW4 通道4 电压设定", "DW4 通道4"), "电压设定"
        )

    def test_four_signals_of_one_supply_become_one_row(self) -> None:
        specs = manual.build_device_specs(dw_entries())

        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.label, "DW4 通道4")
        self.assertEqual([e["signal"] for e in spec.setpoints], [DW_SETPOINT])
        self.assertEqual([e["signal"] for e in spec.toggles], [DW_SWITCH])
        self.assertEqual([e["signal"] for e in spec.readbacks], [DW_VOLTAGE, DW_CURRENT])

    def test_devices_keep_their_first_appearance_order(self) -> None:
        specs = manual.build_device_specs(
            [
                entry("hv.b.voltage_setpoint", writable=True, role="setpoint"),
                entry("hv.a.voltage_setpoint", writable=True, role="setpoint"),
                entry("hv.b.voltage_readback"),
            ]
        )

        self.assertEqual([spec.device for spec in specs], ["hv.b", "hv.a"])

    def test_explicit_device_id_groups_signals_without_name_convention(self) -> None:
        specs = manual.build_device_specs(
            [
                entry("custom.set", device_id="supply-1", device_label="自定义电源"),
                entry("other.read", device_id="supply-1", device_label="自定义电源"),
            ]
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].device, "supply-1")
        self.assertEqual(specs[0].label, "自定义电源")

    def test_hidden_entries_are_not_rendered_and_order_is_configurable(self) -> None:
        specs = manual.build_device_specs(
            [
                entry("dev.b.read", display_order=20),
                entry("dev.hidden.read", visible=False, display_order=0),
                entry("dev.a.read", display_order=10),
            ]
        )

        self.assertEqual([spec.device for spec in specs], ["dev.a", "dev.b"])

    def test_writable_entry_with_unknown_role_is_presented_read_only(self) -> None:
        spec = manual.build_device_specs([entry("x.y", writable=True, role="")])[0]

        self.assertEqual(spec.controls, [])
        self.assertEqual([e["signal"] for e in spec.readbacks], ["x.y"])

    def test_every_entry_lands_exactly_once(self) -> None:
        entries = dw_entries() + [
            entry("detector.fc1.beam_current", label="FC1 束流电流", unit="nA"),
            entry("magnet.m1.start", label="磁铁1 启动", writable=True, role="pulse",
                  min_value=0.0, max_value=1.0),
        ]

        specs = manual.build_device_specs(entries)

        seen = [signal for spec in specs for signal in spec.signals()]
        self.assertEqual(sorted(seen), sorted(e["signal"] for e in entries))

    def test_two_setpoints_make_a_taller_row(self) -> None:
        one = manual.build_device_specs(dw_entries())[0]
        two = manual.build_device_specs(
            dw_entries()
            + [entry("hv_array.dw04.rate_setpoint", label="DW4 通道4 速率设定",
                     writable=True, role="setpoint", min_value=0.0, max_value=10.0)]
        )[0]

        self.assertEqual(one.slot_count(), 1)
        self.assertEqual(two.slot_count(), 2)
        self.assertEqual(two.row_height(), one.row_height() + manual.SLOT_STACK_STEP)

    def test_unit_of_a_signal_comes_from_its_entry(self) -> None:
        spec = manual.build_device_specs(dw_entries())[0]

        self.assertEqual(spec.unit_of(DW_SETPOINT), "V")
        self.assertEqual(spec.unit_of("nope"), "")

    def test_label_of_a_signal_is_the_full_quantity_name(self) -> None:
        spec = manual.build_device_specs(dw_entries())[0]

        self.assertEqual(spec.label_of(DW_SETPOINT), "DW4 通道4 电压设定")


class ColumnAssignmentTests(unittest.TestCase):
    """三列分列：按 PV 命名空间认列，中列留给内容最多的那组。"""

    def test_hv_array_goes_to_the_middle_column(self) -> None:
        specs = manual.build_device_specs(dw_entries())

        self.assertEqual(manual.column_of(specs), 1)

    def test_bd_and_magnet_go_to_the_right_column(self) -> None:
        for device in ("hv_bd.cylinder1", "magnet.m1"):
            with self.subTest(device=device):
                specs = manual.build_device_specs(
                    [entry(f"{device}.voltage_readback", label="X 电压回读")]
                )
                self.assertEqual(manual.column_of(specs), 2)

    def test_other_groups_go_to_the_left_column(self) -> None:
        specs = manual.build_device_specs(
            [entry("gas.ar.flow_setpoint", label="Ar 流量设定")]
        )

        self.assertEqual(manual.column_of(specs), 0)

    def test_assignment_covers_every_group(self) -> None:
        groups = [
            ("气体流量", manual.build_device_specs([entry("gas.ar.mode", label="Ar 气流模式")])),
            ("高压阵列 DW", manual.build_device_specs(dw_entries())),
            ("磁铁电源", manual.build_device_specs(
                [entry("magnet.m1.start", label="磁铁1 启动")])),
        ]

        assignment = manual.assign_columns(groups)

        self.assertEqual(assignment, {"气体流量": 0, "高压阵列 DW": 1, "磁铁电源": 2})

    def test_biggest_group_takes_the_middle_when_no_namespace_matches(self) -> None:
        """命名空间全认不出来时，中列不能让给空着——行数最多的顶上。"""
        small = manual.build_device_specs([entry("a.one", label="A 一")])
        big = manual.build_device_specs(
            [entry(f"b.dev{i}.value", label=f"B{i} 值") for i in range(4)]
        )

        assignment = manual.assign_columns([("小组", small), ("大组", big)])

        self.assertEqual(assignment["大组"], 1)

    def test_an_explicit_middle_is_not_overridden(self) -> None:
        big = manual.build_device_specs(
            [entry(f"x.dev{i}.value", label=f"X{i} 值") for i in range(4)]
        )
        dw = manual.build_device_specs(dw_entries())

        assignment = manual.assign_columns([("大组", big), ("高压阵列 DW", dw)])

        self.assertEqual(assignment["高压阵列 DW"], 1)
        self.assertEqual(assignment["大组"], 0)


class SlotLabelTests(unittest.TestCase):
    """设定槽表头取自映射：用量名，组内不一致时退回「设定N」。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_header_uses_the_quantity_name(self) -> None:
        specs = manual.build_device_specs(dw_entries())

        self.assertEqual(manual._slot_labels(specs), ["电压设定"])

    def test_header_has_one_label_per_slot(self) -> None:
        specs = manual.build_device_specs(
            [
                entry("magnet.m1.current_setpoint", label="磁铁1 电流设定",
                      writable=True, role="setpoint", min_value=0.0, max_value=600.0),
                entry("magnet.m1.rate_setpoint", label="磁铁1 电流速率设定",
                      writable=True, role="setpoint", min_value=0.0, max_value=10.0),
                entry("magnet.m1.current_readback", label="磁铁1 电流回读", unit="A"),
            ]
        )

        self.assertEqual(manual._slot_labels(specs), ["电流设定", "电流速率设定"])

    def test_header_falls_back_when_quantities_differ(self) -> None:
        specs = manual.build_device_specs(
            [
                entry("a.voltage_setpoint", label="A 电压设定", writable=True,
                      role="setpoint", min_value=0.0, max_value=1.0),
                entry("a.voltage_readback", label="A 电压回读"),
                entry("b.current_setpoint", label="B 电流设定", writable=True,
                      role="setpoint", min_value=0.0, max_value=1.0),
                entry("b.current_readback", label="B 电流回读"),
            ]
        )

        self.assertEqual(manual._slot_labels(specs), ["设定1"])

    def test_header_row_keeps_the_fixed_column_widths(self) -> None:
        header = manual._header_row(["电压设定"])

        labels = {lb.text(): lb for lb in header.findChildren(QLabel)}
        self.assertIn("设备", labels)
        self.assertIn("回读", labels)
        self.assertIn("输出", labels)
        self.assertEqual(labels["设备"].minimumWidth(), manual.NAME_WIDTH)
        self.assertEqual(labels["回读"].minimumWidth(), manual.READBACK_WIDTH)
        self.assertEqual(labels["输出"].minimumWidth(), manual.OUTPUT_WIDTH)

    def test_three_columns_fit_the_window_budget(self) -> None:
        """三列 + 列间距不能超过最小可用宽度，否则会被截断。"""
        self.assertEqual(
            manual.MIN_HOLDER_WIDTH,
            manual.COLUMN_COUNT * manual.COLUMN_MIN_WIDTH
            + (manual.COLUMN_COUNT - 1) * manual.COLUMN_SPACING,
        )
        self.assertLessEqual(
            manual.NAME_WIDTH
            + manual.SLOT_WIDTH
            + manual.READBACK_WIDTH
            + manual.OUTPUT_WIDTH
            + 3 * manual.ROW_SPACING,
            manual.COLUMN_MIN_WIDTH,
        )

    def test_row_budget_fits_the_real_window(self) -> None:
        """客户端窗口 1745 宽：减侧栏、页面边距与滚动条后，三列必须放得下。"""
        available = 1745 - 168 - 36 - 14

        self.assertLessEqual(manual.MIN_HOLDER_WIDTH, available)


class AllOffPlanTests(unittest.TestCase):
    """全部关断：先断输出再退设定值，脉冲信号不参与。"""

    def test_toggles_are_issued_before_setpoints(self) -> None:
        plan = manual._all_off_plan(
            [
                entry("s1", writable=True, role="setpoint", min_value=0.0),
                entry("t1", writable=True, role="toggle", min_value=0.0, max_value=1.0),
            ]
        )

        self.assertEqual([signal for signal, _ in plan], ["t1", "s1"])

    def test_setpoints_go_to_their_own_lower_bound(self) -> None:
        plan = manual._all_off_plan(
            [entry("s1", writable=True, role="setpoint", min_value=5.0)]
        )

        self.assertEqual(plan, [("s1", 5.0)])

    def test_setpoint_without_lower_bound_falls_back_to_zero(self) -> None:
        plan = manual._all_off_plan([entry("s1", writable=True, role="setpoint")])

        self.assertEqual(plan, [("s1", 0.0)])

    def test_setpoint_prefers_explicit_safe_value(self) -> None:
        plan = manual._all_off_plan(
            [entry("s1", writable=True, role="setpoint", min_value=0.0, safe_value=12.0)]
        )

        self.assertEqual(plan, [("s1", 12.0)])

    def test_pulse_signals_are_excluded(self) -> None:
        """脉冲写 0 没有语义，还可能触发另一次动作，必须排除。"""
        plan = manual._all_off_plan(
            [
                entry("p1", writable=True, role="pulse", min_value=0.0, max_value=1.0),
                entry("t1", writable=True, role="toggle", min_value=0.0, max_value=1.0),
            ]
        )

        self.assertEqual([signal for signal, _ in plan], ["t1"])

    def test_rate_protection_signal_is_not_zeroed_by_all_off(self) -> None:
        plan = manual._all_off_plan(
            [
                entry(
                    "magnet.m1.current_setpoint",
                    writable=True,
                    role="setpoint",
                    rate_signal="magnet.m1.current_rate_setpoint",
                ),
                entry(
                    "magnet.m1.current_rate_setpoint",
                    writable=True,
                    role="setpoint",
                ),
            ]
        )

        self.assertEqual([signal for signal, _ in plan], ["magnet.m1.current_setpoint"])

    def test_read_only_signals_are_excluded(self) -> None:
        self.assertEqual(manual._all_off_plan([entry("r1")]), [])


class SetpointEditorTests(unittest.TestCase):
    """设定槽：滚轮调值、下发才提交、步进档由页面统一给。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make(self) -> tuple[manual._SetpointEditor, list[float]]:
        submitted: list[float] = []
        editor = manual._SetpointEditor(
            entry("g", label="G 电压设定", writable=True, role="setpoint", unit="V",
                  min_value=0.0, max_value=500.0),
            submitted.append,
        )
        return editor, submitted

    def test_base_step_and_decimals_come_from_the_range(self) -> None:
        editor, _ = self.make()

        self.assertEqual(editor.base_step, 1.0)
        self.assertEqual(editor.spin.decimals(), 1)
        self.assertEqual(editor.spin.singleStep(), 1.0)

    def test_wheel_adjusts_the_value_without_submitting(self) -> None:
        """滚轮只改待下发的数字——改值自动下发是事故来源。"""
        editor, submitted = self.make()
        editor.spin.setValue(100.0)

        wheel(editor.spin, 1)

        self.assertEqual(editor.spin.value(), 101.0)
        self.assertEqual(submitted, [])

    def test_wheel_down_subtracts(self) -> None:
        editor, _ = self.make()
        editor.spin.setValue(100.0)

        wheel(editor.spin, -2)

        self.assertEqual(editor.spin.value(), 98.0)

    def test_wheel_respects_the_range(self) -> None:
        editor, _ = self.make()
        editor.spin.setValue(500.0)

        wheel(editor.spin, 3)

        self.assertEqual(editor.spin.value(), 500.0)

    def test_wheel_on_a_disabled_editor_does_nothing(self) -> None:
        editor, _ = self.make()
        editor.spin.setValue(100.0)
        editor.set_enabled(False)

        wheel(editor.spin, 1)

        self.assertEqual(editor.spin.value(), 100.0)

    def test_send_button_submits_the_edited_value(self) -> None:
        editor, submitted = self.make()
        editor.spin.setValue(123.0)

        editor.send_button.click()

        self.assertEqual(submitted, [123.0])

    def test_editing_alone_never_submits(self) -> None:
        editor, submitted = self.make()

        editor.spin.setValue(123.0)

        self.assertEqual(submitted, [])

    def test_page_step_level_rescales_the_editor(self) -> None:
        editor, _ = self.make()

        editor.set_level(10)

        self.assertEqual(editor.spin.singleStep(), 10.0)
        self.assertEqual(editor.level(), 10)

    def test_reading_backfills_the_setpoint(self) -> None:
        editor, _ = self.make()

        editor.apply_reading(240.0)

        self.assertEqual(editor.spin.value(), 240.0)

    def test_missing_reading_leaves_the_setpoint_alone(self) -> None:
        editor, _ = self.make()
        editor.spin.setValue(60.0)

        editor.apply_reading(None)

        self.assertEqual(editor.spin.value(), 60.0)


class DeviceRowTests(unittest.TestCase):
    """一行一个设备：控件构成、开关按钮同步、回读平铺、被拒标红。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def make_row(self, spec: manual.DeviceSpec | None = None) -> manual._DeviceRow:
        spec = spec or manual.build_device_specs(dw_entries())[0]
        return manual._DeviceRow(spec, self.page)

    def spec_of(self, entries: list[dict], index: int = 0) -> manual.DeviceSpec:
        return manual.build_device_specs(entries)[index]

    def test_row_carries_all_four_signals(self) -> None:
        row = self.make_row()

        self.assertEqual(row.label_text, "DW4 通道4")
        self.assertEqual(row.writable_signals(), [DW_SETPOINT, DW_SWITCH])
        self.assertEqual(row.readback_signals(), [DW_VOLTAGE, DW_CURRENT])

    def test_one_setpoint_slot_and_one_switch_pair(self) -> None:
        row = self.make_row()

        self.assertEqual(len(row.editors), 1)
        self.assertEqual(len(row.switches), 1)

    def test_two_setpoints_stack_two_slots_in_one_row(self) -> None:
        spec = self.spec_of(
            [
                entry("magnet.m1.current_setpoint", label="磁铁1 电流设定", writable=True,
                      role="setpoint", unit="A", min_value=0.0, max_value=600.0),
                entry("magnet.m1.rate_setpoint", label="磁铁1 电流速率设定", writable=True,
                      role="setpoint", unit="A/s", min_value=0.0, max_value=10.0),
                entry("magnet.m1.current_readback", label="磁铁1 电流回读", unit="A"),
            ]
        )
        row = manual._DeviceRow(spec, self.page)

        self.assertEqual(len(row.editors), 2)
        self.assertGreater(row.minimumHeight(), manual.ROW_HEIGHT)

    def test_switch_is_a_button_pair(self) -> None:
        row = self.make_row()
        _signal, on, off = row.switches[0]

        self.assertEqual((on.text(), off.text()), ("开", "关"))
        self.assertTrue(on.isCheckable())

    def test_switch_buttons_follow_the_actual_state(self) -> None:
        """实际开着而按钮显示关，点一次就误发关断。"""
        row = self.make_row()

        row.apply_readings(dw_readings(switch=1.0))

        _signal, on, off = row.switches[0]
        self.assertTrue(on.isChecked())
        self.assertFalse(off.isChecked())

    def test_switch_buttons_track_a_closed_output(self) -> None:
        row = self.make_row()

        row.apply_readings(dw_readings(voltage=0.0, switch=0.0))

        _signal, on, off = row.switches[0]
        self.assertFalse(on.isChecked())
        self.assertTrue(off.isChecked())

    def test_clicking_open_writes_one_to_the_switch_signal(self) -> None:
        row = self.make_row()
        written: list[tuple[str, float]] = []
        self.page.write_signal = lambda _row, s, v: written.append((s, v))  # type: ignore[method-assign]

        row.switches[0][1].click()

        self.assertEqual(written, [(DW_SWITCH, 1.0)])

    def test_clicking_close_writes_zero_to_the_switch_signal(self) -> None:
        row = self.make_row()
        written: list[tuple[str, float]] = []
        self.page.write_signal = lambda _row, s, v: written.append((s, v))  # type: ignore[method-assign]

        row.switches[0][2].click()

        self.assertEqual(written, [(DW_SWITCH, 0.0)])

    def test_clicking_a_pulse_writes_one(self) -> None:
        spec = self.spec_of(
            [
                entry("magnet.m1.current_setpoint", label="磁铁1 电流设定", writable=True,
                      role="setpoint", unit="A", min_value=0.0, max_value=600.0),
                entry("magnet.m1.current_readback", label="磁铁1 电流回读", unit="A"),
                entry("magnet.m1.start", label="磁铁1 启动", writable=True, role="pulse",
                      min_value=0.0, max_value=1.0),
            ]
        )
        row = manual._DeviceRow(spec, self.page)
        written: list[tuple[str, float]] = []
        self.page.write_signal = lambda _row, s, v: written.append((s, v))  # type: ignore[method-assign]

        self.assertEqual(row.triggers[0].text(), "启动")
        row.triggers[0].click()

        self.assertEqual(written, [("magnet.m1.start", 1.0)])

    def test_integer_mode_toggle_becomes_a_number_box(self) -> None:
        """气流模式量程 0-2，用开/关按钮会丢掉中间档。"""
        spec = self.spec_of(
            [
                entry("gas.ar.flow_setpoint", label="Ar 流量设定", writable=True,
                      role="setpoint", unit="sccm", min_value=0.0, max_value=500.0),
                entry("gas.ar.flow_readback", label="Ar 瞬时流量", unit="sccm"),
                entry("gas.ar.mode", label="Ar 气流模式", writable=True, role="toggle",
                      min_value=0.0, max_value=2.0),
            ]
        )
        row = manual._DeviceRow(spec, self.page)

        self.assertEqual(row.switches, [])
        self.assertEqual(len(row.modes), 1)
        _entry, spin, _send = row.modes[0]
        self.assertEqual(spin.decimals(), 0)

    def test_mode_box_follows_the_actual_value(self) -> None:
        spec = self.spec_of(
            [
                entry("gas.ar.flow_setpoint", label="Ar 流量设定", writable=True,
                      role="setpoint", unit="sccm", min_value=0.0, max_value=500.0),
                entry("gas.ar.mode", label="Ar 气流模式", writable=True, role="toggle",
                      min_value=0.0, max_value=2.0),
            ]
        )
        row = manual._DeviceRow(spec, self.page)

        row.apply_readings(
            {"gas.ar.flow_setpoint": connected(10.0), "gas.ar.mode": connected(2.0)}
        )

        self.assertEqual(row.modes[0][1].value(), 2.0)

    def test_readbacks_are_tiled_on_one_line(self) -> None:
        row = self.make_row()

        row.apply_readings(dw_readings())

        # 前缀是稳定标记：≈ = 回读已进入容差（状态不能只靠颜色表达）
        self.assertEqual(row.readback_label.text(), "≈ 2000 V · 1.5 mA")

    def test_extra_readbacks_are_summarised_instead_of_clipping(self) -> None:
        spec = self.spec_of(
            [
                entry("d.s", label="D 设定", writable=True, role="setpoint",
                      min_value=0.0, max_value=10.0),
                *[entry(f"d.rb{i}", label=f"D 读数{i}", unit="V") for i in range(4)],
            ]
        )
        row = manual._DeviceRow(spec, self.page)
        readings = {"d.s": connected(1.0)}
        readings.update({f"d.rb{i}": connected(float(i)) for i in range(4)})

        row.apply_readings(readings)

        self.assertIn("0 V · 1 V · +2", row.readback_label.text())
        self.assertIn("d.rb3", row.readback_label.toolTip())

    def test_read_only_device_has_no_controls(self) -> None:
        spec = self.spec_of(
            [entry("detector.fc1.beam_current", label="FC1 束流电流", unit="nA")]
        )
        row = manual._DeviceRow(spec, self.page)

        row.apply_readings({"detector.fc1.beam_current": connected(12.5)})

        self.assertEqual(row.readback_label.text(), "12.5 nA")
        self.assertEqual(row.controls(), [])

    def test_disconnected_device_disables_every_control(self) -> None:
        row = self.make_row()

        row.apply_readings({DW_SETPOINT: offline(), DW_SWITCH: offline()})

        self.assertEqual(row.readback_label.text(), "未连接")
        self.assertFalse(row.editors[0].send_button.isEnabled())
        self.assertFalse(row.switches[0][1].isEnabled())

    def test_one_dead_writable_signal_disables_the_row(self) -> None:
        """执行层不区分哪一路掉线时，宁可整行保守禁用。"""
        row = self.make_row()

        row.apply_readings({DW_SETPOINT: connected(100.0), DW_SWITCH: offline()})

        self.assertFalse(row.switches[0][1].isEnabled())

    def test_rejected_write_marks_the_row_red_with_the_reason(self) -> None:
        row = self.make_row()
        row.apply_readings(dw_readings())

        row.mark_rejected("被拒绝：超过上限 5100 V")

        self.assertEqual(row.readback_label.property("state"), "error")
        self.assertIn("超过上限", row.readback_label.toolTip())

    def test_a_new_write_clears_the_previous_rejection_mark(self) -> None:
        row = self.make_row()
        row.apply_readings(dw_readings())
        row.mark_rejected("被拒绝")

        row.clear_rejection()

        self.assertEqual(row.readback_label.property("state"), "")

    def test_quantity_label_is_the_full_signal_label(self) -> None:
        row = self.make_row()

        self.assertEqual(row.quantity_label(DW_SETPOINT), "DW4 通道4 电压设定")


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def test_page_is_registered_in_control_section(self) -> None:
        specs = {spec.key: spec for spec in page_specs()}

        self.assertIn("manual", specs)
        self.assertEqual(specs["manual"].label, "手动控制")
        self.assertEqual(specs["manual"].section, "control")
        self.assertEqual(specs["manual"].icon, "control")

    def test_page_has_three_columns_and_no_group_tabs(self) -> None:
        """对齐 demo：单页三列，不是按设备组分页签。"""
        self.assertEqual(len(self.page._columns), 3)
        self.assertFalse(hasattr(self.page, "group_tabs"))

    def test_mapping_builds_one_row_per_device(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        self.assertEqual(len(self.page._rows), 1)
        self.assertTrue(self.page.all_off_button.isEnabled())

    def test_panel_lands_in_the_middle_column(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        panels = [
            sum(1 for i in range(column.count()) if column.itemAt(i).widget() is not None)
            for column in self.page._columns
        ]

        self.assertEqual(panels, [0, 1, 0])

    def test_every_signal_of_a_device_points_at_its_row(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        for signal in DW_SIGNALS:
            with self.subTest(signal=signal):
                self.assertIn(signal, self.page._rows_by_signal)
        self.assertIs(
            self.page._rows_by_signal[DW_SETPOINT], self.page._rows_by_signal[DW_VOLTAGE]
        )

    def test_mapping_failure_reports_reason_and_disables_all_off(self) -> None:
        self.page._on_mapping({"ok": False, "message": "连接被拒绝", "config": None})

        self.assertFalse(self.page.all_off_button.isEnabled())
        self.assertIn("连接被拒绝", self.page.status_label.text())

    def test_snapshot_reaches_every_row_and_the_topbar(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        self.page._on_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": DW_SETPOINT, "connected": True, "value": 2000.0},
                        {"signal": DW_SWITCH, "connected": True, "value": 1.0},
                        {"signal": DW_VOLTAGE, "connected": True, "value": 1998.0},
                        {"signal": DW_CURRENT, "connected": True, "value": 2.0},
                    ]
                },
            }
        )

        row = self.page._rows[0]
        self.assertEqual(row.readback_label.text(), "≈ 1998 V · 2 mA")
        self.assertEqual(row.editors[0].spin.value(), 2000.0)

    def test_detector_group_moves_to_the_topbar_readouts(self) -> None:
        """demo 顶栏就是 FC1/FC2 大字读数，束流探测不再占一列。"""
        self.page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": dw_entries()
                    + [
                        entry("detector.fc1.beam_current", label="FC1 束流电流",
                              unit="nA", group="束流探测"),
                        entry("detector.fc2.beam_current", label="FC2 束流电流",
                              unit="nA", group="束流探测"),
                    ]
                },
            }
        )

        self.assertEqual(len(self.page.topbar.readouts), 2)
        self.page._on_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "detector.fc1.beam_current", "connected": True,
                         "value": 12.5},
                        {"signal": "detector.fc2.beam_current", "connected": True,
                         "value": 8.0},
                    ]
                },
            }
        )

        values = [label.text() for _entry, label in self.page.topbar.readouts]
        # 顶栏读数固定两位小数，位数不再随数值变化（否则整条顶栏每秒抖）
        self.assertEqual(values, ["12.50 nA", "8.00 nA"])

    def test_topbar_falls_back_to_a_panel_when_there_are_too_many_readouts(self) -> None:
        many = [
            entry(f"detector.fc{i}.beam_current", label=f"FC{i} 束流电流", unit="nA",
                  group="束流探测")
            for i in range(manual.TOPBAR_SLOTS + 1)
        ]

        self.page._on_mapping({"ok": True, "config": {"entries": many}})

        self.assertEqual(self.page.topbar.readouts, [])
        self.assertEqual(len(self.page._rows), manual.TOPBAR_SLOTS + 1)

    def test_step_level_applies_to_every_editor(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        editor = self.page._rows[0].editors[0]

        self.page.set_step_level(1)

        self.assertEqual(self.page.step_level(), 10)
        self.assertEqual(editor.spin.singleStep(), editor.base_step * 10)

    def test_write_busy_is_reported_instead_of_dropped(self) -> None:
        from apps.desktop_client import instrument_api

        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        row = self.page._rows[0]
        original = instrument_api._write_busy
        instrument_api._write_busy = True
        try:
            self.page.write_signal(row, DW_SETPOINT, 10.0)
        finally:
            instrument_api._write_busy = original

        self.assertIn("上一次写入尚未完成", self.page.status_label.text())

    def test_accepted_write_is_reported_with_the_quantity_name(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        row = self.page._rows[0]
        self.page._pending = (row, DW_SETPOINT, 2000.0)

        self.page._on_write_result(
            {
                "ok": True,
                "payload": {
                    "accepted": True,
                    "applied": 2000.0,
                    "readback": 1999.0,
                    "ramp_steps": [1000.0, 2000.0],
                },
            }
        )

        message = self.page.status_label.text()
        self.assertIn("DW4 通道4 电压设定", message)
        self.assertIn("已下发 2000 V", message)
        self.assertIn("斜坡 2 步", message)

    def test_rejected_write_keeps_the_reason_and_marks_the_row(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        row = self.page._rows[0]
        self.page._pending = (row, DW_SETPOINT, 9000.0)

        self.page._on_write_result(
            {"ok": True, "payload": {"accepted": False, "reason": "超过上限 5100 V"}}
        )

        self.assertIn("被拒绝：超过上限 5100 V", self.page.status_label.text())
        self.assertEqual(row.readback_label.property("state"), "error")

    def test_operation_active_covers_server_side_all_off(self) -> None:
        self.assertFalse(self.page.is_operation_active())

        self.page._all_off_active = True
        self.assertTrue(self.page.is_operation_active())

        self.page.safe_stop()

        self.assertTrue(self.page.is_operation_active())
        self.assertIn("服务端执行", self.page.status_label.text())

    def test_all_off_reports_server_batch_failure(self) -> None:
        self.page._all_off_active = True
        self.page._on_all_off_result(
            {
                "ok": True,
                "payload": {"ok": False, "message": "整批未下发：设备不在远程"},
            }
        )

        self.assertIn("整批未下发", self.page.status_label.text())
        self.assertTrue(self.page.all_off_button.isEnabled())

    def test_all_off_is_sent_as_one_atomic_batch(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        sent: dict = {}

        def fake(writes, *, note="", atomic=True, base_url=None):
            sent.update({"writes": writes, "note": note, "atomic": atomic})
            return _GroupPanelThread()

        with mock.patch.object(instrument_api, "request_batch_write", fake):
            self.page.start_all_off()

        self.assertTrue(sent["atomic"])
        self.assertEqual(
            [item["signal"] for item in sent["writes"]],
            [DW_SWITCH, DW_SETPOINT],
        )


class SeriesHelperTests(unittest.TestCase):
    """趋势绘图辅助：缺口断线、历史最优。"""

    def test_series_is_passed_through_when_there_are_no_gaps(self) -> None:
        xs, ys = manual.series_with_gaps([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)], 5.0)

        self.assertEqual(xs, [0.0, 1.0, 2.0])
        self.assertEqual(ys, [1.0, 2.0, 3.0])

    def test_a_gap_inserts_nan_so_the_line_breaks(self) -> None:
        """轮询断一拍不能在图上画成一条直线——那会把"中断"显示成"平稳"。"""
        xs, ys = manual.series_with_gaps([(0.0, 1.0), (1.0, 2.0), (30.0, 3.0)], 5.0)

        self.assertEqual(len(xs), 4)
        self.assertEqual(xs[2], 15.5)
        self.assertNotEqual(ys[2], ys[2])  # NaN
        self.assertEqual((ys[0], ys[1], ys[3]), (1.0, 2.0, 3.0))

    def test_empty_series_stays_empty(self) -> None:
        self.assertEqual(manual.series_with_gaps([], 5.0), ([], []))

    def test_best_so_far_is_a_running_maximum(self) -> None:
        self.assertEqual(manual.best_so_far([1.0, 3.0, 2.0, 5.0, 4.0]),
                         [1.0, 3.0, 3.0, 5.0, 5.0])

    def test_best_so_far_skips_nan(self) -> None:
        result = manual.best_so_far([1.0, float("nan"), 2.0])

        self.assertEqual(result[0], 1.0)
        self.assertEqual(result[1], 1.0)
        self.assertEqual(result[2], 2.0)


class FormatAndTimeTests(unittest.TestCase):
    """固定宽度读数与时间戳解析。"""

    def test_fmt_keeps_fixed_decimals(self) -> None:
        self.assertEqual(manual._fmt_fixed(12.5, "nA"), "12.50 nA")
        self.assertEqual(manual._fmt_fixed(8.0, "nA"), "8.00 nA")
        self.assertEqual(manual._fmt_fixed(8.0, "nA", 1), "8.0 nA")

    def test_fmt_fixed_handles_missing_values(self) -> None:
        self.assertEqual(manual._fmt_fixed(None), manual.EMPTY)
        self.assertEqual(manual._fmt_fixed("abc"), manual.EMPTY)

    def test_fmt_still_trims_for_the_dense_table(self) -> None:
        self.assertEqual(manual._fmt(12.5, "nA"), "12.5 nA")
        self.assertEqual(manual._fmt(8.0, "nA"), "8 nA")

    def test_parse_time_accepts_utc_iso(self) -> None:
        self.assertIsNotNone(manual._parse_time("2026-09-11T04:00:00+00:00"))
        self.assertIsNotNone(manual._parse_time("2026-09-11T04:00:00Z"))

    def test_parse_time_treats_naive_as_utc(self) -> None:
        self.assertEqual(
            manual._parse_time("2026-09-11T04:00:00"),
            manual._parse_time("2026-09-11T04:00:00+00:00"),
        )

    def test_parse_time_rejects_junk(self) -> None:
        for value in (None, "", "  ", "not-a-time", 123):
            with self.subTest(value=value):
                self.assertIsNone(manual._parse_time(value))

    def test_lag_uses_received_time_then_source_time(self) -> None:
        now = manual._parse_time("2026-09-11T04:00:10+00:00")
        assert now is not None

        lag = manual._lag_seconds({"received_time": "2026-09-11T04:00:05+00:00"}, now)
        fallback = manual._lag_seconds({"source_time": "2026-09-11T04:00:07+00:00"}, now)

        self.assertAlmostEqual(lag, 5.0)
        self.assertAlmostEqual(fallback, 3.0)

    def test_lag_is_none_without_timestamps(self) -> None:
        self.assertIsNone(manual._lag_seconds({}))


class SettleAndStaleTests(unittest.TestCase):
    """稳定标记与滞后标记：状态必须有文字，不能只靠颜色。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def make_row(self, **overrides: object) -> manual._DeviceRow:
        spec = manual.build_device_specs(dw_entries(**overrides))[0]
        return manual._DeviceRow(spec, self.page)

    @staticmethod
    def plan(setpoint: float, readback: float, *, stamp: str | None = None) -> dict:
        """设定 PV 与回读 PV 取不同的值——稳定判定比的是这两个。

        注意 `apply_readings` 会先用**设定 PV 的值**回填设定框，所以"设定框 vs 回读"
        实际就是"下发的命令值 vs 实际回读"，这正是想比的东西。
        """
        reading = {"connected": True, "value": readback}
        if stamp is not None:
            reading["received_time"] = stamp
        setpoint_reading = {"connected": True, "value": setpoint}
        if stamp is not None:
            setpoint_reading["received_time"] = stamp
        return {
            DW_SETPOINT: setpoint_reading,
            DW_SWITCH: {"connected": True, "value": 1.0},
            DW_VOLTAGE: reading,
            DW_CURRENT: dict(reading),
        }

    def test_settled_readback_gets_the_marker(self) -> None:
        row = self.make_row()

        row.apply_readings(self.plan(2000.0, 2000.0))

        self.assertTrue(row.readback_label.text().startswith("≈"))
        self.assertEqual(row.readback_label.property("state"), "good")

    def test_deviating_readback_gets_the_other_marker(self) -> None:
        row = self.make_row()

        row.apply_readings(self.plan(2000.0, 1500.0))

        self.assertTrue(row.readback_label.text().startswith("≠"))
        self.assertEqual(row.readback_label.property("state"), "warn")

    def test_mapping_settle_tolerance_wins_over_the_heuristic(self) -> None:
        """容差用映射里的 settle_tol——执行层与扫谱/调束判稳用的是同一套阈值。"""
        row = make_row_with_tol(self.page, tol=300.0)

        row.apply_readings(self.plan(2000.0, 1750.0))

        self.assertEqual(row.readback_label.property("state"), "good")

    def test_mapping_settle_tolerance_can_flag_what_percent_would_pass(self) -> None:
        row = make_row_with_tol(self.page, tol=1.0)

        row.apply_readings(self.plan(2000.0, 1990.0))

        self.assertEqual(row.readback_label.property("state"), "warn")

    def test_stale_reading_is_marked_with_text(self) -> None:
        row = self.make_row()
        old = "2020-01-01T00:00:00+00:00"

        row.apply_readings(self.plan(1.0, 1.0, stamp=old))

        self.assertIn("滞后", row.readback_label.text())
        self.assertEqual(row.readback_label.property("state"), "warn")

    def test_fresh_reading_is_not_marked_stale(self) -> None:
        from datetime import UTC, datetime

        row = self.make_row()

        row.apply_readings(self.plan(1.0, 1.0, stamp=datetime.now(UTC).isoformat()))

        self.assertNotIn("滞后", row.readback_label.text())

    def test_an_offline_row_says_so_instead_of_a_row_of_dashes(self) -> None:
        row = self.make_row()

        row.apply_readings({DW_SETPOINT: offline(), DW_SWITCH: offline()})

        self.assertEqual(row.readback_label.text(), "未连接")


def make_row_with_tol(page: manual.ManualControlPage, tol: float) -> manual._DeviceRow:
    entries = dw_entries()
    entries[0]["settle_tol"] = tol
    spec = manual.build_device_specs(entries)[0]
    return manual._DeviceRow(spec, page)


class OutputCellFitTests(unittest.TestCase):
    """输出格必须装得下它的按钮——曾经 80px 装 104–114px 的按钮，被裁掉了。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def make_row(self, entries: list[dict]) -> manual._DeviceRow:
        return manual._DeviceRow(manual.build_device_specs(entries)[0], self.page)

    def assert_output_fits(self, row: manual._DeviceRow) -> None:
        if row.output_layout is None:
            return
        cell = row.output_layout.parentWidget()
        kids = [w for w in row.controls() if w.parentWidget() is cell]
        if not kids:
            return
        needed = sum(w.width() for w in kids) + row.output_layout.spacing() * (len(kids) - 1)
        with self.subTest(needed=needed):
            self.assertLessEqual(needed, manual.OUTPUT_WIDTH)
            self.assertLessEqual(needed, cell.width())

    def test_three_pulse_buttons_fit(self) -> None:
        """磁铁：启动/停止/复位。"""
        row = self.make_row(
            [
                entry("magnet.m1.current_setpoint", label="磁铁1 电流设定", writable=True,
                      role="setpoint", unit="A", min_value=0.0, max_value=600.0),
                entry("magnet.m1.current_readback", label="磁铁1 电流回读", unit="A"),
                *[entry(f"magnet.m1.{name}", label=f"磁铁1 {caption}", writable=True,
                        role="pulse", min_value=0.0, max_value=1.0)
                  for name, caption in (("start", "启动"), ("stop", "停止"), ("reset", "复位"))],
            ]
        )

        self.assertEqual(len(row.triggers), 3)
        self.assert_output_fits(row)

    def test_switch_pair_plus_pulse_fits(self) -> None:
        """溅射电源：开/关 + 灭弧。"""
        row = self.make_row(
            [
                entry("sputter.power_setpoint", label="溅射功率设定", writable=True,
                      role="setpoint", unit="W", min_value=0.0, max_value=500.0),
                entry("sputter.power_readback", label="溅射功率回读", unit="W"),
                entry("sputter.power_enable", label="溅射电源使能", writable=True,
                      role="toggle", min_value=0.0, max_value=1.0),
                entry("sputter.arc_clear", label="灭弧", writable=True, role="pulse",
                      min_value=0.0, max_value=1.0),
            ]
        )

        self.assertEqual(len(row.switches), 1)
        self.assertEqual(len(row.triggers), 1)
        self.assert_output_fits(row)

    def test_integer_mode_box_fits(self) -> None:
        """气路：模式整数框 + 下发。"""
        row = self.make_row(
            [
                entry("gas.ar.flow_setpoint", label="Ar 流量设定", writable=True,
                      role="setpoint", unit="sccm", min_value=0.0, max_value=500.0),
                entry("gas.ar.flow_readback", label="Ar 瞬时流量", unit="sccm"),
                entry("gas.ar.mode", label="Ar 气流模式", writable=True, role="toggle",
                      min_value=0.0, max_value=2.0),
            ]
        )

        self.assertEqual(len(row.modes), 1)
        self.assert_output_fits(row)


class CardVisibilityTests(unittest.TestCase):
    """每张卡片都必须真的显示出来。

    这里用 `isHidden()` 而不是 `isVisible()`：测试里页面没有 show()，所有控件的
    `isVisible()` 都是 False，只有"是否被显式隐藏"能分辨出漏调 show() 的卡片。
    这个断言是给一次真实事故上的锁——重构放置逻辑时把 `show()` 弄丢了，
    结果除趋势卡片外所有设备卡片都从界面上消失了。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def test_all_cards_are_shown(self) -> None:
        self.page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": dw_entries()
                    + [
                        entry("detector.fc1.beam_current", label="FC1 束流电流",
                              unit="nA", group="束流探测"),
                    ]
                },
            }
        )

        hidden = [f._key for f in self.page._frames if f.isHidden()]

        self.assertEqual(hidden, [], f"这些卡片没显示：{hidden}")

    def test_a_card_exists_for_every_group_not_taken_by_the_topbar(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        self.assertEqual(len(self.page._frames), 1)
        self.assertFalse(self.page._frames[0].isHidden())

    def test_trend_card_is_shown_too(self) -> None:
        self.page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": dw_entries()
                    + [
                        entry("detector.fc1.beam_current", label="FC1 束流电流",
                              unit="nA", group="束流探测"),
                    ]
                },
            }
        )

        trend_frame = next(f for f in self.page._frames if f._key == "__trend__")

        self.assertFalse(trend_frame.isHidden())

    def test_cards_do_not_overlap_within_a_column(self) -> None:
        """同一列的卡片不能叠在一起（否则看起来就是"少了几张"）。"""
        entries = []
        for index in range(4):
            entries.extend(
                [
                    entry(f"gas.g{index}.flow_setpoint", label=f"气路{index} 流量设定",
                          writable=True, role="setpoint", unit="sccm",
                          min_value=0.0, max_value=500.0, group="气体流量"),
                    entry(f"gas.g{index}.flow_readback", label=f"气路{index} 瞬时流量",
                          unit="sccm", group="气体流量"),
                ]
            )
        self.page._on_mapping({"ok": True, "config": {"entries": entries}})

        frames = sorted(self.page._frames, key=lambda f: f.y())
        for upper, lower in zip(frames, frames[1:]):
            with self.subTest(upper=upper._key, lower=lower._key):
                self.assertLessEqual(upper.y() + upper.height(), lower.y())


class LayoutPersistenceTests(unittest.TestCase):
    """卡片位置持久化与"恢复布局"。

    布局存储换成临时 INI 文件：受限环境里注册表写入会返回 `Status.AccessError`
    并被静默丢弃，那样"存-取-清"这条链路根本没法验证（也会污染用户真实设置）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self._tmp.name) / "layout.ini"), QSettings.Format.IniFormat
        )
        self._real_factory = manual.layout_settings
        manual.layout_settings = lambda: self.settings
        self.addCleanup(self._restore_factory)

        self.page = manual.ManualControlPage()
        self.page._canvas.resize(1400, 900)

    def _restore_factory(self) -> None:
        manual.layout_settings = self._real_factory
        self._tmp.cleanup()
        self.page.deleteLater()

    def make_frame(self, key: str = "测试卡片") -> manual._DraggableFrame:
        panel = manual._GroupPanel(key)
        return self.page._place_panel(panel, key=key, column=0, height=150)

    def test_parse_saved_rect_accepts_list_and_str(self) -> None:
        self.assertEqual(manual._parse_saved_rect([1, 2, 3, 4]), (1, 2, 3, 4))
        self.assertEqual(manual._parse_saved_rect("[1, 2, 3, 4]"), (1, 2, 3, 4))
        self.assertEqual(manual._parse_saved_rect("1 2 3 4"), (1, 2, 3, 4))
        self.assertEqual(manual._parse_saved_rect([1.0, 2.0, 3.0, 4.0]), (1, 2, 3, 4))

    def test_parse_saved_rect_rejects_junk(self) -> None:
        for value in (None, "", "abc", [1, 2, 3], [1, 2, 3, 4, 5], object()):
            with self.subTest(value=value):
                self.assertIsNone(manual._parse_saved_rect(value))

    def test_saved_rect_is_restored_on_the_next_build(self) -> None:
        frame = self.make_frame()
        frame.setGeometry(300, 120, 420, 200)

        frame.save_geometry()
        again = self.make_frame()

        self.assertEqual(
            (again.x(), again.y(), again.width(), again.height()),
            (300, 120, 420, 200),
        )

    def test_a_rect_outside_the_canvas_is_clamped_back_in(self) -> None:
        """跨窗口尺寸沿用旧坐标会被钳回画布内，而不是摆到看不见的地方。"""
        self.settings.setValue(
            f"{manual.LAYOUT_SETTINGS_PREFIX}测试卡片/rect", [1200, 10, 900, 400]
        )

        frame = self.make_frame()

        # 画布 1400 宽、卡片 900 宽 → x 钳到右缘 500；y 与尺寸不变
        self.assertEqual(
            (frame.x(), frame.y(), frame.width(), frame.height()),
            (500, 10, 900, 400),
        )

    def test_a_negative_or_tiny_rect_is_clamped(self) -> None:
        """负坐标钳到 0，小于最小尺寸的卡片放大到最小尺寸。"""
        min_w = manual._DraggableFrame.MIN_W
        min_h = manual._DraggableFrame.MIN_H
        cases = (
            ([-50, 10, 420, 200], (0, 10, 420, 200)),
            ([10, -50, 420, 200], (10, 0, 420, 200)),
            ([10, 10, 100, 200], (10, 10, min_w, 200)),
            ([10, 10, 420, 50], (10, 10, 420, min_h)),
        )
        for rect, expected in cases:
            with self.subTest(rect=rect):
                self.settings.setValue(
                    f"{manual.LAYOUT_SETTINGS_PREFIX}测试卡片/rect", rect
                )
                frame = self.make_frame()
                self.assertEqual(
                    (frame.x(), frame.y(), frame.width(), frame.height()),
                    expected,
                )

    def test_vertical_overflow_is_kept_horizontal_is_clamped(self) -> None:
        """纵向越界存档保留 y（画布滚动查看），横向越界钳回画布内。"""
        self.settings.setValue(
            f"{manual.LAYOUT_SETTINGS_PREFIX}测试卡片/rect", [1200, 800, 460, 200]
        )
        self.page._canvas.resize(862, 640)
        frame = self.make_frame()
        # x 钳回画布内（862-460=402）；y=800 超出画布高度但保留，靠滚动条查看
        self.assertEqual((frame.x(), frame.y()), (402, 800))
        # 画布最低高度被撑到能看到这张卡片
        self.assertGreaterEqual(
            self.page._canvas.minimumHeight(), 800 + 200 + manual.CANVAS_MARGIN
        )

    def test_saved_layout_is_restored_after_canvas_is_laid_out(self) -> None:
        """卡片在页面显示前构建时，恢复被钳到初始画布内；显示后统一恢复到位。

        真实时序：映射请求在 __init__ 发出，用户停留在初始化页时卡片已构建，
        那时 canvas 还是初始尺寸（862x640），存档 (700,700) 放不下会被钳制。
        页面显示、画布按窗口尺寸布局后再恢复一次，位置就对了。
        """
        # 存档在真实画布（1400x900）内放得下，但在 hidden 初始画布（862x640）放不下
        self.settings.setValue(
            f"{manual.LAYOUT_SETTINGS_PREFIX}测试卡片/rect", [700, 700, 460, 200]
        )
        # 不 resize canvas：保持 hidden 构建时的初始尺寸
        self.page._canvas.resize(862, 640)
        frame = self.make_frame()
        # 横向钳回画布内（左右锁死）；纵向保留越界值（画布滚动查看）
        self.assertEqual((frame.x(), frame.y()), (862 - 460, 700))

        # 页面显示、画布布局完成 → 统一恢复
        self.page._canvas.resize(1400, 900)
        self.page._restore_saved_layout()

        self.assertEqual((frame.x(), frame.y()), (700, 700))

    def test_restore_reports_whether_it_applied(self) -> None:
        frame = self.make_frame()

        self.assertFalse(frame.restore_geometry(self.page._canvas))  # 没有存档
        self.settings.setValue(
            f"{manual.LAYOUT_SETTINGS_PREFIX}测试卡片/rect", [20, 30, 420, 200]
        )
        frame.setGeometry(0, 0, 300, 150)

        self.assertTrue(frame.restore_geometry(self.page._canvas))
        self.assertEqual((frame.x(), frame.y()), (20, 30))

    def test_reset_layout_clears_the_saved_positions(self) -> None:
        frame = self.make_frame()
        frame.setGeometry(300, 120, 420, 200)
        frame.save_geometry()
        self.page._entries = dw_entries()

        self.page.reset_layout()

        remaining = [
            key
            for key in self.settings.allKeys()
            if key.startswith(manual.LAYOUT_SETTINGS_PREFIX)
        ]
        self.assertEqual(remaining, [])
        self.assertIn("恢复默认", self.page.status_label.text())

    def test_reset_layout_is_wired_to_a_button(self) -> None:
        self.assertIsNotNone(self.page.topbar.reset_layout_button)
        self.page.topbar.reset_layout_button.click()  # 不该抛异常

    def test_trend_card_sits_in_the_middle_column(self) -> None:
        """中列只有 DW 一张，放得下趋势卡；左列放不下（会超出一屏）。"""
        self.page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": dw_entries()
                    + [
                        entry("detector.fc1.beam_current", label="FC1 束流电流",
                              unit="nA", group="束流探测"),
                    ]
                },
            }
        )

        trend = next(f for f in self.page._frames if f._key == "__trend__")

        self.assertEqual(trend.property("layoutColumn"), 1)

    def test_default_columns_fit_the_users_window_height(self) -> None:
        """默认布局下每列高度都要放得进一屏（用户窗口约 946 高 → 视口约 730）。"""
        viewport_height = 730
        expected = (
            ("气体流量", 120, 0),
            ("溅射电源", 120, 0),
            ("腔体真空", 120, 0),
            ("聚焦/漂移管", 164, 0),
            ("__trend__", manual._TrendPanel.HEIGHT, 1),
            ("高压阵列 DW", 416, 1),
            ("新高压电源 BD", 216, 2),
            ("磁铁电源", 260, 2),
        )
        columns = [manual.CANVAS_MARGIN] * manual.COLUMN_COUNT
        for _name, height, column in expected:
            columns[column] += height + manual.CARD_GAP

        for index, height in enumerate(columns):
            with self.subTest(column=index):
                self.assertLessEqual(height, viewport_height)


class TrendCardTests(unittest.TestCase):
    """束流电流趋势卡片。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def detector_entries(self) -> list[dict]:
        return [
            entry("detector.fc1.beam_current", label="FC1 束流电流", unit="nA",
                  group="束流探测"),
            entry("detector.fc2.beam_current", label="FC2 束流电流", unit="nA",
                  group="束流探测"),
        ]

    def load(self) -> manual._TrendPanel:
        self.page._on_mapping(
            {"ok": True, "config": {"entries": dw_entries() + self.detector_entries()}}
        )
        panel = self.page.trend_panel
        assert panel is not None
        return panel

    def feed(self, panel: manual._TrendPanel, samples: int = 5) -> None:
        for index in range(samples):
            self.page._on_snapshot(
                {
                    "ok": True,
                    "payload": {
                        "readings": [
                            {"signal": "detector.fc1.beam_current", "connected": True,
                             "value": 10.0 + index, "received_time": iso(index)},
                            {"signal": "detector.fc2.beam_current", "connected": True,
                             "value": 5.0 + index, "received_time": iso(index)},
                        ]
                    },
                }
            )

    def test_card_is_created_only_when_the_topbar_has_readouts(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})

        self.assertIsNone(self.page.trend_panel)

    def test_card_binds_the_topbar_signals(self) -> None:
        panel = self.load()

        self.assertEqual(
            [e["signal"] for e in panel.signals],
            ["detector.fc1.beam_current", "detector.fc2.beam_current"],
        )

    def test_default_window_is_two_minutes(self) -> None:
        panel = self.load()

        self.assertEqual(panel.window_s, 120.0)
        self.assertEqual(panel.WINDOWS[1], (120.0, "2min"))

    def test_window_buttons_switch_the_span(self) -> None:
        panel = self.load()

        panel.window_buttons[0].click()

        self.assertEqual(panel.window_s, 30.0)

    def test_clear_empties_the_buffer(self) -> None:
        panel = self.load()
        self.feed(panel)
        self.assertTrue(self.page.trend.signals())

        panel.clear_button.click()

        self.assertEqual(self.page.trend.signals(), [])

    def test_best_toggle_is_off_by_default(self) -> None:
        panel = self.load()

        self.assertFalse(panel.show_best)

        panel.best_button.click()

        self.assertTrue(panel.show_best)

    def test_stats_line_reports_the_window(self) -> None:
        panel = self.load()
        self.feed(panel, 5)

        text = panel.stats_label.text()

        self.assertIn("FC1 束流电流", text)
        self.assertIn("σ", text)
        self.assertIn("点/", text)

    def test_card_has_no_step_buttons(self) -> None:
        """趋势卡片不需要 ×1/×10/×100 步进档。"""
        panel = self.load()

        self.assertEqual(panel.step_buttons, [])

    def test_card_is_not_marked_as_hazard(self) -> None:
        panel = self.load()

        self.assertFalse(panel.property("hazard"))


def iso(offset_seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=offset_seconds)).isoformat()


class TrendRecordingTests(unittest.TestCase):
    """快照推进滚动缓冲与顶栏变化量。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()
        self.page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": dw_entries()
                    + [
                        entry("detector.fc1.beam_current", label="FC1 束流电流", unit="nA",
                              group="束流探测"),
                    ]
                },
            }
        )

    def tearDown(self) -> None:
        self.page.deleteLater()

    def snapshot(self, value: float) -> None:
        self.page._on_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "detector.fc1.beam_current", "connected": True,
                         "value": value, "received_time": iso(0)},
                    ]
                },
            }
        )

    def test_snapshot_records_into_the_buffer(self) -> None:
        self.snapshot(12.5)

        self.assertEqual(self.page.trend.signal_points("detector.fc1.beam_current"), 1)

    def test_zero_samples_are_recorded_not_treated_as_gaps(self) -> None:
        """0 是合法读数，不能和"没读到"混为一谈。"""
        self.snapshot(0.0)

        self.assertEqual(self.page.trend.signal_points("detector.fc1.beam_current"), 1)

    def test_disconnected_signal_is_not_recorded(self) -> None:
        self.page._on_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "detector.fc1.beam_current", "connected": False,
                         "value": None},
                    ]
                },
            }
        )

        self.assertEqual(self.page.trend.signals(), [])

    def test_failed_poll_does_not_touch_the_buffer(self) -> None:
        self.snapshot(1.0)

        self.page._on_snapshot({"ok": False, "message": "连接被拒绝"})

        self.assertEqual(self.page.trend.signal_points("detector.fc1.beam_current"), 1)

    def test_topbar_shows_a_fixed_width_delta(self) -> None:
        self.page.trend.push("detector.fc1.beam_current", 10.0, time.time() - 40.0)
        self.snapshot(12.5)

        _entry, label = self.page.topbar.readouts[0]
        trend = self.page.topbar.readout_trends[0]

        self.assertEqual(label.text(), "12.50 nA")
        self.assertIn("▲", trend.text())
        self.assertIn("+2.5", trend.text())

    def test_topbar_marks_a_stale_reading(self) -> None:
        self.page._on_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "detector.fc1.beam_current", "connected": True,
                         "value": 3.0, "received_time": "2020-01-01T00:00:00+00:00"},
                    ]
                },
            }
        )

        _entry, label = self.page.topbar.readouts[0]
        trend = self.page.topbar.readout_trends[0]

        self.assertEqual(label.property("state"), "warn")
        self.assertIn("滞后", trend.text())

    def test_topbar_has_one_sparkline_per_readout(self) -> None:
        self.assertEqual(len(self.page.topbar.readout_sparks), 1)
        self.assertEqual(
            self.page.topbar.readout_sparks[0]._signal, "detector.fc1.beam_current"
        )

    def test_sparkline_paints_with_and_without_data(self) -> None:
        spark = self.page.topbar.readout_sparks[0]
        spark.resize(manual._Sparkline.WIDTH, manual._Sparkline.HEIGHT)
        spark.grab()  # 无数据：画基线

        self.snapshot(1.0)
        self.snapshot(2.0)
        spark.grab()  # 有数据：画折线

    def test_trend_card_follows_the_snapshot(self) -> None:
        self.snapshot(1.0)
        self.snapshot(2.0)

        panel = self.page.trend_panel
        assert panel is not None
        self.assertIn("2", panel.stats_label.text())


class WidthBudgetTests(unittest.TestCase):
    """列宽预算：输出格装得下最宽的按钮组合，且三列能放进现场最小屏。

    这组断言是给"按钮被裁掉"那个 bug 上的锁：以前 OUTPUT_WIDTH=80 而实际需要
    104–114px，磁铁/溅射/气路三张卡片的按钮在界面上是缺一块的。
    """

    def test_three_pulses_fit_the_output_cell(self) -> None:
        needed = 3 * manual.PULSE_WIDTH + 2 * manual.SLOT_SPACING

        self.assertLessEqual(needed, manual.OUTPUT_WIDTH)

    def test_switch_pair_plus_pulse_fits_the_output_cell(self) -> None:
        needed = (
            2 * manual.TOGGLE_WIDTH + manual.PULSE_WIDTH + 2 * manual.SLOT_SPACING
        )

        self.assertLessEqual(needed, manual.OUTPUT_WIDTH)

    def test_integer_mode_box_fits_the_output_cell(self) -> None:
        needed = manual.MODE_SPIN_WIDTH + manual.SEND_WIDTH + manual.SLOT_SPACING

        self.assertLessEqual(needed, manual.OUTPUT_WIDTH)

    def test_setpoint_slot_is_wide_enough_for_the_longest_number(self) -> None:
        """"10000.0" 这种七位数字要放得下（左右箭头已按 QSS 让位）。"""
        digits = 7 * 8  # 13px 等宽数字约 8px/字符，留点余量
        self.assertLessEqual(digits + 8, manual.SPIN_WIDTH)
        self.assertLessEqual(manual.SPIN_WIDTH + manual.SEND_WIDTH + manual.SLOT_SPACING,
                             manual.SLOT_WIDTH)

    def test_three_columns_fit_the_smallest_onsite_screen(self) -> None:
        """1600x1000 满窗（侧栏 168 + 页面边距 36 + 滚动条 14）下不许横向滚动。"""
        available = 1600 - 168 - 36 - 14
        needed = manual.MIN_HOLDER_WIDTH + 2 * manual.CANVAS_MARGIN

        self.assertLessEqual(needed, available)

    def test_row_budget_equals_the_column_minimum(self) -> None:
        self.assertLessEqual(
            manual.NAME_WIDTH
            + manual.SLOT_WIDTH
            + manual.READBACK_WIDTH
            + manual.OUTPUT_WIDTH
            + 3 * manual.ROW_SPACING,
            manual.COLUMN_MIN_WIDTH,
        )
        self.assertEqual(
            manual.MIN_HOLDER_WIDTH,
            manual.COLUMN_COUNT * manual.COLUMN_MIN_WIDTH
            + (manual.COLUMN_COUNT - 1) * manual.COLUMN_SPACING,
        )


class DeviceStateUnknownTests(unittest.TestCase):
    """`device_state_unknown` 是安全字段，必须显性告警。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = manual.ManualControlPage()
        self.page._on_mapping({"ok": True, "config": {"entries": dw_entries()}})
        self.row = self.page._rows[0]

    def tearDown(self) -> None:
        self.page.deleteLater()

    def test_unknown_state_is_reported_as_an_error_and_marks_the_row(self) -> None:
        self.page._pending = (self.row, DW_SETPOINT, 2000.0)

        self.page._on_write_result(
            {
                "ok": True,
                "payload": {
                    "accepted": True,
                    "applied": 2000.0,
                    "device_state_unknown": True,
                },
            }
        )

        message = self.page.status_label.text()
        self.assertIn("设备状态未知", message)
        self.assertIn("现场核对", message)
        self.assertEqual(self.page.status_label.property("state"), "error")
        self.assertEqual(self.row.readback_label.property("state"), "error")

    def test_a_normal_accepted_write_stays_green(self) -> None:
        self.page._pending = (self.row, DW_SETPOINT, 2000.0)

        self.page._on_write_result(
            {"ok": True, "payload": {"accepted": True, "applied": 2000.0}}
        )

        self.assertNotIn("设备状态未知", self.page.status_label.text())
        self.assertEqual(self.page.status_label.property("state"), "good")


def magnet_entries() -> list[dict]:
    """四台磁铁（设定 + 速率 + 回读），够成组设定面板用。"""
    rows: list[dict] = []
    for n in (1, 2, 3, 4):
        rows += [
            entry(
                f"magnet.m{n}.current_setpoint",
                label=f"磁铁{n} 电流设定",
                writable=True,
                role="setpoint",
                unit="A",
                group="磁铁电源",
                readback_signal=f"magnet.m{n}.current_readback",
                rate_signal=f"magnet.m{n}.current_rate_setpoint",
                min_value=0.0,
                max_value=600.0,
                max_step=100.0,
            ),
            entry(
                f"magnet.m{n}.current_rate_setpoint",
                label=f"磁铁{n} 速率设定",
                writable=True,
                role="setpoint",
                unit="A/s",
                group="磁铁电源",
                min_value=0.0,
                max_value=10.0,
            ),
            entry(
                f"magnet.m{n}.current_readback",
                label=f"磁铁{n} 电流回读",
                unit="A",
                group="磁铁电源",
            ),
        ]
    return rows


class MagnetGroupPanelTests(unittest.TestCase):
    """成组设定：组定义只有一份、整批下发、失败要说清是哪一台。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_page(self, entries: list[dict] | None = None) -> manual.ManualControlPage:
        page = manual.ManualControlPage()
        page._on_mapping(
            {
                "ok": True,
                "config": {
                    "entries": entries if entries is not None else magnet_entries()
                },
            }
        )
        return page

    def test_group_choices_use_the_shared_link_group_definition(self) -> None:
        from packages.domain import beamline

        choices = manual._magnet_group_choices()

        self.assertEqual(len(choices), len(beamline.LINKED_VARIABLE_SETS))
        for (label, signals), linked in zip(choices, beamline.LINKED_VARIABLE_SETS):
            self.assertEqual(signals, [s for s in linked if s.endswith("_setpoint")])
            self.assertTrue(label.startswith("磁铁"))
            self.assertTrue(label.endswith("同步"))

    def test_panel_is_built_when_the_mapping_has_the_magnets(self) -> None:
        page = self.make_page()

        panel = page.magnet_group_panel
        self.assertIsNotNone(panel)
        self.assertEqual(panel.group_combo.count(), len(manual._magnet_group_choices()))

    def test_panel_is_skipped_without_magnet_signals(self) -> None:
        page = self.make_page(
            [entry("gas.ar.flow_setpoint", writable=True, role="setpoint")]
        )

        self.assertIsNone(page.magnet_group_panel)

    def test_batch_request_carries_every_member_of_the_group(self) -> None:
        page = self.make_page()
        panel = page.magnet_group_panel
        panel.group_combo.setCurrentIndex(len(manual._magnet_group_choices()) - 1)
        panel.current_spin.setValue(120.0)
        sent: dict = {}

        def fake(writes, *, note="", atomic=True, base_url=None):
            sent.update({"writes": writes, "note": note, "atomic": atomic})
            return _GroupPanelThread()

        with mock.patch.object(instrument_api, "request_batch_write", fake):
            panel._submit()

        self.assertTrue(sent["atomic"])
        self.assertEqual(
            [item["signal"] for item in sent["writes"]],
            [f"magnet.m{n}.current_setpoint" for n in (1, 2, 3, 4)],
        )
        self.assertTrue(all(item["value"] == 120.0 for item in sent["writes"]))
        self.assertIn("1+2+3+4", sent["note"])

    def test_rate_is_sent_before_the_current_when_enabled(self) -> None:
        page = self.make_page()
        panel = page.magnet_group_panel
        panel.group_combo.setCurrentIndex(0)  # 磁铁1+2
        panel.current_spin.setValue(150.0)
        panel.rate_check.setChecked(True)
        panel.rate_spin.setValue(4.0)
        sent: dict = {}

        def fake(writes, *, note="", atomic=True, base_url=None):
            sent["writes"] = writes
            return _GroupPanelThread()

        with mock.patch.object(instrument_api, "request_batch_write", fake):
            panel._submit()

        self.assertEqual(
            [item["signal"] for item in sent["writes"]],
            [
                "magnet.m1.current_rate_setpoint",
                "magnet.m2.current_rate_setpoint",
                "magnet.m1.current_setpoint",
                "magnet.m2.current_setpoint",
            ],
        )

    def test_partial_success_names_the_failing_member(self) -> None:
        page = self.make_page()
        panel = page.magnet_group_panel

        panel._on_done(
            {
                "ok": True,
                "payload": {
                    "ok": False,
                    "message": "成组写入部分成功：3 路已下发、1 路失败",
                    "results": [
                        {"signal": "magnet.m1.current_setpoint", "accepted": True},
                        {
                            "signal": "magnet.m2.current_setpoint",
                            "accepted": False,
                            "reason": "CA 写入超时",
                        },
                    ],
                },
            }
        )

        text = panel.result_label.text()
        self.assertIn("部分成功", text)
        self.assertIn("magnet.m2.current_setpoint", text)
        self.assertIn("CA 写入超时", text)
        self.assertEqual(panel.result_label.property("state"), "error")

    def test_transport_failure_is_never_shown_as_success(self) -> None:
        page = self.make_page()
        panel = page.magnet_group_panel

        panel._on_done({"ok": False, "message": "设备组当前不可用：磁铁电源"})

        self.assertIn("未执行", panel.result_label.text())
        self.assertIn("设备组当前不可用", panel.result_label.text())
        self.assertEqual(panel.result_label.property("state"), "error")

    def test_busy_write_is_reported_instead_of_dropped(self) -> None:
        page = self.make_page()
        panel = page.magnet_group_panel

        with mock.patch.object(
            instrument_api, "request_batch_write", lambda *a, **k: None
        ):
            panel._submit()

        self.assertIn("上一次写入", panel.result_label.text())


if __name__ == "__main__":
    unittest.main()
