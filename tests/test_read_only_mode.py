"""全局只读部署模式（改造报告 §8.9 P2.2）：一个部署参数关掉**所有**写入。

原 demo 用 ``--read-only`` 禁用全部写控件。这里对应的做法是把开关放在**执行服务**
上（环境变量 ``SPECTRUM_READ_ONLY``），因为客户端可以有很多个、也可能有人直接调
HTTP——只把界面按钮变灰挡不住任何一次真实写入。

断言分三层：
1. **执行层**：单点写、成组写、回落全部被拒，且设备值一点没动；
2. **接口层**：扫谱/调束启动与 PV 映射保存直接 400，状态接口把标记报出来；
3. **界面层**：手动页、扫谱页、调束页、设置页把会写设备的控件压住并说明原因。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi import HTTPException
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from apps.desktop_client import initialization as initialization_module
from apps.desktop_client import instrument_api
from apps.desktop_client.pages import manual as manual_module
from apps.desktop_client.pages import scan as scan_module
from apps.desktop_client.pages import settings as settings_module
from apps.desktop_client.pages.tuning import TuningPage
from apps.instrument_service.app import create_app
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.runtime import READ_ONLY_ENV, InstrumentRuntime, read_only_from_env
from apps.instrument_service.scan_store import ScanStore
from packages.contracts import (
    MagnetRetractRequest,
    PvMappingConfig,
    ScanAxis,
    ScanRunRequest,
    SignalWriteRequest,
    TuningRunRequest,
    TuningVariable,
)

MAGNETS = (1, 2, 3, 4)
SETPOINTS = [f"magnet.m{n}.current_setpoint" for n in MAGNETS]
READBACKS = [f"magnet.m{n}.current_readback" for n in MAGNETS]
RATE_SIGNALS = [f"magnet.m{n}.current_rate_setpoint" for n in MAGNETS]

BEAM = "detector.fc1.beam_current"


def setpoint_entry(n: int) -> dict:
    return {
        "signal": f"magnet.m{n}.current_setpoint",
        "label": f"磁铁{n} 电流设定",
        "pv": f"BD:DipoleMagnet:{n:02d}:CurrentSet",
        "unit": "A",
        "writable": True,
        "required": False,
        "group": "磁铁电源",
        "role": "setpoint",
        "readback_signal": f"magnet.m{n}.current_readback",
        "rate_signal": f"magnet.m{n}.current_rate_setpoint",
        "min_value": 0.0,
        "max_value": 600.0,
        "max_step": 100.0,
        "max_rate": 1e6,
        "settle_tol": 0.5,
        "settle_timeout": 5.0,
        "tunable": True,
    }


def readback_entry(n: int) -> dict:
    return {
        "signal": f"magnet.m{n}.current_readback",
        "label": f"磁铁{n} 电流回读",
        "pv": f"BD:DipoleMagnet:{n:02d}:CurrentMonitor",
        "unit": "A",
        "writable": False,
        "required": False,
        "group": "磁铁电源",
        "role": "readback",
    }


def rate_entry(n: int) -> dict:
    return {
        "signal": f"magnet.m{n}.current_rate_setpoint",
        "label": f"磁铁{n} 速率设定",
        "pv": f"BD:DipoleMagnet:{n:02d}:CurrentRateSet",
        "unit": "A/s",
        "writable": True,
        "required": False,
        "group": "磁铁电源",
        "role": "rate",
        "min_value": 0.0,
        "max_value": 10.0,
        "max_step": 10.0,
        "max_rate": 1e6,
    }


def beam_entry() -> dict:
    return {
        "signal": BEAM,
        "label": "FC1 束流电流",
        "pv": "BD:FC:01:BeamCurrent",
        "unit": "nA",
        "writable": False,
        "required": True,
        "group": "束流探测",
        "role": "readback",
        "beam_target": True,
    }


CONFIG = {
    "version": 1,
    "entries": (
        [setpoint_entry(n) for n in MAGNETS]
        + [readback_entry(n) for n in MAGNETS]
        + [rate_entry(n) for n in MAGNETS]
        + [beam_entry()]
    ),
}


def route_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"未找到路由 {method} {path}")


class _ReadOnlyRuntimeCase(unittest.TestCase):
    """一个只读执行服务 + 一个可写执行服务（用来证明拦住写入的确实是这个开关）。"""

    read_only = True

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.runtime = self._make(self.read_only)

    def _make(self, read_only: bool) -> InstrumentRuntime:
        return InstrumentRuntime(
            PvMappingConfig.model_validate(CONFIG),
            gateway_factory=create_simulated_gateway,
            store=ScanStore(Path(self._directory.name)),
            read_only=read_only,
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self._directory.cleanup()

    def readback(self, n: int) -> float:
        return float(self.runtime.gateway().read(f"magnet.m{n}.current_readback").value)

    def readbacks(self) -> list[float]:
        """四路磁铁的实际回读：用「写前后不变」证明写入一次都没发生。"""
        return [self.readback(n) for n in MAGNETS]

    def write(self, signal: str, value: float):
        return self.runtime.signals.write(SignalWriteRequest(signal=signal, value=value))


class ReadOnlyWritesTests(_ReadOnlyRuntimeCase):
    """执行层：只读部署下每一次写入都必须被拒，且设备状态不受影响。"""

    def test_single_write_is_rejected_with_a_readable_reason(self) -> None:
        before = self.readback(1)

        result = self.write(SETPOINTS[0], 100.0)

        self.assertFalse(result.accepted)
        self.assertIn("全局只读模式", result.reason)
        self.assertIn("重启服务", result.reason)
        # 被拒的写入不该留下"设备状态未知"的悬念
        self.assertFalse(result.device_state_unknown)
        self.assertEqual(self.readback(1), before)

    def test_rejection_does_not_depend_on_the_value_being_valid(self) -> None:
        """越界值也一样只报只读：先答"不能写"，不必再答"这个值是否合法"。"""
        result = self.write(SETPOINTS[0], 5000.0)

        self.assertFalse(result.accepted)
        self.assertIn("全局只读模式", result.reason)
        self.assertNotIn("上限", result.reason)

    def test_batch_write_writes_nothing_at_all(self) -> None:
        before = self.readbacks()

        response = self.runtime.write_batch(
            [SignalWriteRequest(signal=s, value=120.0) for s in SETPOINTS]
        )

        self.assertFalse(response.ok)
        self.assertTrue(response.nothing_written)
        self.assertEqual((response.accepted, response.rejected), (0, 4))
        for result in response.results:
            self.assertFalse(result.accepted)
            self.assertIn("全局只读模式", result.reason)
        self.assertEqual(self.readbacks(), before)

    def test_retract_reports_every_channel_as_rejected(self) -> None:
        """回落是安全动作，也只读拒绝——但要说清"一路都没退"，不能报成功。"""
        before = self.readbacks()
        before_rate = self.runtime.gateway().read(RATE_SIGNALS[0]).value

        outcome = self.runtime.retract_magnets(SETPOINTS, 0.0, 2.0, timeout_s=1.0)

        self.assertFalse(outcome.ok)
        self.assertIn("回落写入被拒", outcome.message)
        self.assertIn("全局只读模式", outcome.message)
        self.assertEqual(outcome.applied, {})
        self.assertEqual(self.readbacks(), before)
        # 速率也是写：一条都不该漏
        self.assertEqual(self.runtime.gateway().read(RATE_SIGNALS[0]).value, before_rate)

    def test_retract_endpoint_returns_200_with_ok_false(self) -> None:
        app = create_app(self.runtime)
        retract = route_endpoint(app, "/control/v1/magnets/retract", "POST")

        response = retract(
            MagnetRetractRequest(
                setpoint_signals=SETPOINTS, current_a=0.0, rate_a_s=2.0, timeout_s=1.0
            )
        )

        self.assertFalse(response.ok)
        self.assertIn("全局只读模式", response.message)

    def test_the_same_writes_go_through_when_the_flag_is_off(self) -> None:
        """对照组：同样的配置、同样的请求，不加只读参数就能写——拦住写入的是开关本身。"""
        writable = self._make(read_only=False)
        self.addCleanup(writable.close)

        result = writable.signals.write(SignalWriteRequest(signal=SETPOINTS[0], value=100.0))

        self.assertTrue(result.accepted, result.reason)
        self.assertAlmostEqual(
            float(writable.gateway().read(READBACKS[0]).value), 100.0, places=3
        )


class ReadOnlyEndpointsTests(_ReadOnlyRuntimeCase):
    """接口层：进行不下去的动作在**启动阶段**就说清，而不是跑一半才报错。"""

    def setUp(self) -> None:
        super().setUp()
        self.before = self.readbacks()
        self.app = create_app(self.runtime)

    def test_status_reports_the_flag(self) -> None:
        status = route_endpoint(self.app, "/control/v1/status", "GET")()

        self.assertTrue(status.read_only)
        self.assertIn("全局只读", status.detail)

    def test_scan_start_is_rejected(self) -> None:
        endpoint = route_endpoint(self.app, "/control/v1/scan/runs", "POST")
        request = ScanRunRequest(
            axis=ScanAxis(
                label="磁铁 1~4",
                setpoint_signals=SETPOINTS,
                readback_signal=READBACKS[0],
            ),
            detector_signal=BEAM,
            start=0.0,
            stop=50.0,
            step=10.0,
        )

        with self.assertRaises(HTTPException) as caught:
            endpoint(request)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("全局只读模式", str(caught.exception.detail))
        # 启动被拒就不该留下任何痕迹：没有占住设备组，也没有磁铁被动过
        self.assertEqual(self.runtime.locks.held(), {})
        self.assertEqual(self.readbacks(), self.before)

    def test_tuning_start_is_rejected(self) -> None:
        endpoint = route_endpoint(self.app, "/control/v1/tuning/runs", "POST")
        request = TuningRunRequest(
            target_signal=BEAM,
            variables=[
                TuningVariable(signal=SETPOINTS[0], label="磁铁1", low=0.0, high=100.0)
            ],
        )

        with self.assertRaises(HTTPException) as caught:
            endpoint(request)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("全局只读模式", str(caught.exception.detail))

    def test_saving_the_pv_mapping_is_rejected(self) -> None:
        """映射决定"谁能写、写到多少"：只读部署下改它等于绕过只读本身。"""
        endpoint = route_endpoint(self.app, "/control/v1/pv-mapping", "PUT")

        with self.assertRaises(HTTPException) as caught:
            endpoint(PvMappingConfig.model_validate(CONFIG))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("全局只读模式", str(caught.exception.detail))

    def test_reading_still_works(self) -> None:
        """只读不是"连不上"：状态、映射、信号读取都必须照常可用。"""
        self.assertEqual(route_endpoint(self.app, "/control/v1/status", "GET")().status, "ready")
        self.assertEqual(
            len(route_endpoint(self.app, "/control/v1/pv-mapping", "GET")().entries),
            len(CONFIG["entries"]),
        )


class ReadOnlyFlagSourceTests(unittest.TestCase):
    """开关来源：部署参数（环境变量），默认关闭。"""

    def test_env_values_that_enable_it(self) -> None:
        for raw in ("1", "true", "TRUE", "yes", "on", " On "):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {READ_ONLY_ENV: raw}):
                self.assertTrue(read_only_from_env())

    def test_env_values_that_do_not(self) -> None:
        for raw in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {READ_ONLY_ENV: raw}):
                self.assertFalse(read_only_from_env())

    def test_runtime_defaults_to_the_environment(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.dict(os.environ, {READ_ONLY_ENV: "on"}):
            runtime = InstrumentRuntime(
                PvMappingConfig.model_validate(CONFIG),
                gateway_factory=create_simulated_gateway,
                store=ScanStore(Path(directory.name)),
            )
        self.addCleanup(runtime.close)

        self.assertTrue(runtime.read_only)


class _FakeSignal:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload
        self._callbacks: list = []

    def connect(self, callback, *_args, **_kwargs) -> None:
        self._callbacks.append(callback)
        if self._payload is not None:
            callback(self._payload)


class _FakeThread:
    """替掉真实请求线程：界面测试不联网、不留 QThread。"""

    def __init__(self, *_args, completed_payload: dict | None = None, **_kwargs) -> None:
        self.completed = _FakeSignal(completed_payload)
        self.finished = _FakeSignal(None)

    def start(self) -> None:
        return None


class _ClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def set_read_only(self, value: bool) -> None:
        instrument_api.set_read_only(value)
        self.addCleanup(instrument_api.set_read_only, False)

    def stub(self, target, name: str, replacement) -> None:
        original = getattr(target, name)
        setattr(target, name, replacement)
        self.addCleanup(setattr, target, name, original)


class InitializationFlagTests(_ClientCase):
    """客户端启动检查：把服务端的只读标记记下来，供各页面判断。

    界面能不能按只是**展示**；真正的强制点在执行层。但如果客户端不把标记带回来，
    操作员看到的就是"按钮能按、按了被拒"，比直接说明白更糟。
    """

    def run_check(self, read_only: bool) -> None:
        def fake_request(url, timeout=3.0):
            if url.endswith("/control/v1/status"):
                return {"service": "instrument-service", "read_only": read_only}
            return {"summary": {"total": 1, "connected": 1}, "items": []}

        self.stub(initialization_module, "_request_json", fake_request)
        worker = initialization_module.InitializationWorker(
            "http://127.0.0.1:1", "http://127.0.0.1:8767", targets=("instrument",)
        )
        worker.run()

    def test_worker_records_the_flag_from_the_service(self) -> None:
        self.set_read_only(False)

        self.run_check(True)

        self.assertTrue(instrument_api.is_read_only())

    def test_worker_clears_the_flag_when_the_service_is_writable(self) -> None:
        """上一次连接的是只读实例、这次不是：标记必须跟着**这次**的答案走。"""
        instrument_api.set_read_only(True)
        self.addCleanup(instrument_api.set_read_only, False)

        self.run_check(False)

        self.assertFalse(instrument_api.is_read_only())


class ManualPageReadOnlyTests(_ClientCase):
    """手动页：写入控件（含成组下发与总断电）全部压住。"""

    def setUp(self) -> None:
        self.set_read_only(True)
        # 页面加载映射后会立刻轮询一次：换成假线程，既不连服务也不留 QThread
        self.stub(instrument_api, "request_read", lambda *a, **k: _FakeThread())
        self.page = manual_module.ManualControlPage()
        self.addCleanup(self.page.deleteLater)

    def load(self) -> None:
        self.page._on_mapping({"ok": True, "config": {"entries": CONFIG["entries"]}})

    @staticmethod
    def unlocked(page) -> list[str]:
        """仍然按得动的写入控件（应当为空）。"""
        return [
            widget.objectName() or type(widget).__name__
            for row in page._rows
            for widget in row.controls()
            if widget.isEnabled()
        ]

    def test_all_write_controls_are_locked_after_mapping_load(self) -> None:
        self.load()

        self.assertTrue(self.page._rows)
        self.assertEqual(self.unlocked(self.page), [])
        self.assertFalse(self.page.topbar.all_off_button.isEnabled())

    def test_magnet_group_panel_explains_why_it_is_locked(self) -> None:
        self.load()

        panel = self.page.magnet_group_panel
        self.assertIsNotNone(panel)
        self.assertFalse(panel.send_button.isEnabled())
        self.assertIn("只读", panel.result_label.text())

    def test_repeated_polls_do_not_unlock_the_controls(self) -> None:
        """每轮快照都会按连接状态启用控件——只读必须能压住后续每一帧。"""
        self.load()
        snapshot = {
            entry["signal"]: {"signal": entry["signal"], "value": 1.0, "connected": True}
            for entry in CONFIG["entries"]
        }

        for row in self.page._rows:
            row.apply_readings(snapshot)

        self.assertEqual(self.unlocked(self.page), [])


class ScanPageReadOnlyTests(_ClientCase):
    """扫谱页：开始扫描与手动回落都要动磁铁，只读下两个入口都不给。"""

    def setUp(self) -> None:
        self.requests: list = []
        for name in ("request_scan_start", "request_magnet_retract", "request_read"):
            self.stub(
                instrument_api,
                name,
                lambda *a, _name=name, **k: self.requests.append(_name) or _FakeThread(),
            )
        self.page = scan_module.ScanPage()
        self.addCleanup(self.page.deleteLater)

    def test_show_event_locks_both_write_entries(self) -> None:
        self.set_read_only(True)

        self.page.show()

        self.assertFalse(self.page.start_button.isEnabled())
        self.assertFalse(self.page.retract_button.isEnabled())
        self.assertIn("全局只读模式", self.page.state_label.text())
        self.assertIn("全局只读模式", self.page.retract_status.text())

    def test_programmatic_start_sends_nothing(self) -> None:
        """按钮变灰挡不住代码调用：发起前也要挡一次，不能只靠界面。"""
        self.set_read_only(True)
        self.page.start_value.setValue(0.0)
        self.page.end_value.setValue(50.0)

        self.page.start_scan()

        self.assertEqual(self.requests, [])
        self.assertIn("全局只读模式", self.page.state_label.text())

    def test_programmatic_retract_sends_nothing(self) -> None:
        self.set_read_only(True)

        self.page._manual_retract()

        self.assertEqual(self.requests, [])
        self.assertIn("全局只读模式", self.page.retract_status.text())

    def test_without_the_flag_the_page_stays_usable(self) -> None:
        self.set_read_only(False)

        self.page.show()

        self.assertTrue(self.page.start_button.isEnabled())
        self.assertNotIn("只读", self.page.state_label.text())


class TuningPageReadOnlyTests(_ClientCase):
    """调束页：启动、确认候选、三种设备处置都要写设备。"""

    def setUp(self) -> None:
        self.requests: list = []
        # 构造页面时就会拉一次设备档案/映射：那两条不算"写入"，单独用不记录的桩挡掉
        for name in ("request_tuning_catalog", "request_read"):
            self.stub(instrument_api, name, lambda *a, **k: _FakeThread())
        for name in (
            "request_tuning_start",
            "request_tuning_approve",
            "request_tuning_finalize",
            "request_tuning_iterations",
        ):
            self.stub(
                instrument_api,
                name,
                lambda *a, _name=name, **k: self.requests.append(_name) or _FakeThread(),
            )
        self.asked: list[str] = []
        self.page = TuningPage()
        self.page._run_id = "run-1"
        self.stub(
            self.page,
            "_ask_finalize_confirm",
            lambda label: self.asked.append(label) or True,
        )
        self.addCleanup(self.page.deleteLater)

    def test_show_event_locks_start_and_explains(self) -> None:
        self.set_read_only(True)

        self.page.show()

        self.assertFalse(self.page.start_button.isEnabled())
        self.assertTrue(self.page.read_only_note.isVisible())
        self.assertIn("全局只读模式", self.page.read_only_note.text())

    def test_programmatic_start_sends_nothing(self) -> None:
        self.set_read_only(True)

        self.page.start_tuning()

        self.assertEqual(self.requests, [])

    def test_approving_a_proposal_sends_nothing(self) -> None:
        self.set_read_only(True)

        self.page._approve()

        self.assertEqual(self.requests, [])
        self.assertIn("全局只读模式", self.page.proposal_label.text())

    def test_finalize_asks_no_confirmation_and_sends_nothing(self) -> None:
        """先挡再问：不能弹了确认框、操作员点了"是"才发现写不了。"""
        self.set_read_only(True)

        self.page.finalize_tuning("restore")

        self.assertEqual(self.asked, [])
        self.assertEqual(self.requests, [])
        self.assertIn("全局只读模式", self.page.finalize_label.text())

    def test_terminal_status_does_not_re_enable_write_buttons(self) -> None:
        """轮询到终态时页面会重新启用按钮——只读必须一起判断。"""
        self.set_read_only(True)
        self.page.show()

        self.page._apply_status({"state": "completed", "iterations": []})

        self.assertFalse(self.page.start_button.isEnabled())
        for button in self.page.finalize_buttons.values():
            self.assertFalse(button.isEnabled())

    def test_pending_proposal_does_not_re_enable_approve(self) -> None:
        self.set_read_only(True)
        self.page.show()

        self.page._apply_status(
            {
                "state": "awaiting_confirmation",
                "pending": {"iteration": 0, "values": {}, "predicted": 1.0, "std": 0.1},
            }
        )

        self.assertFalse(self.page.approve_button.isEnabled())

    def test_without_the_flag_the_page_stays_usable(self) -> None:
        self.set_read_only(False)

        self.page.show()

        self.assertTrue(self.page.start_button.isEnabled())
        self.assertFalse(self.page.read_only_note.isVisible())


class SettingsPageReadOnlyTests(_ClientCase):
    """设置页：映射保存是一条真实写入（它决定写入边界）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.saved: list = []
        self.stub(
            settings_module,
            "PvMappingRequestThread",
            lambda url, payload=None: self.saved.append(payload) or _FakeThread(),
        )
        self.page = settings_module.SystemSettingsPage(
            QSettings(
                str(Path(self._tmp.name) / "settings.ini"), QSettings.Format.IniFormat
            )
        )
        self.addCleanup(self.page.deleteLater)

    def load(self) -> None:
        self.page._finish_pv_request(
            {"ok": True, "config": dict(CONFIG), "message": "", "issues": []}
        )

    def test_save_button_is_locked_after_loading_the_mapping(self) -> None:
        self.set_read_only(True)

        self.load()

        self.assertFalse(self.page.pv_save_button.isEnabled())

    def test_programmatic_save_sends_nothing(self) -> None:
        self.set_read_only(True)
        self.load()

        self.page._save_pv_mapping()

        self.assertEqual(self.saved, [])
        self.assertIn("全局只读模式", self.page.pv_feedback.text())

    def test_without_the_flag_saving_still_starts_a_request(self) -> None:
        self.set_read_only(False)
        self.load()

        self.assertTrue(self.page.pv_save_button.isEnabled())
        self.page._save_pv_mapping()

        self.assertEqual(len(self.saved), 1)


if __name__ == "__main__":
    unittest.main()
