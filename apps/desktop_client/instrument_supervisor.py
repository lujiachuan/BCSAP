"""本机仪器执行服务的自动拉起与善后（操作电脑单文件夹交付用）。

背景：架构上控制逻辑必须跑在独立的 instrument_service 进程里（不放进
PySide6 界面进程）；但对操作员来说不应要求手动开两个程序。本模块让客户端
启动时探测 http://127.0.0.1:8765/control/v1/health/live：不可达且能在分发
目录中找到同目录的 spectrum-instrument-service.exe 时，以隐藏窗口方式拉起
该服务并轮询健康检查直到就绪；找不到服务 exe（纯检索电脑、开发环境）则
什么都不做，只当普通数据客户端。

退出策略可配置：QSettings 键 ``instrument/stopServiceOnExit``，默认 True
（骨架阶段“关界面即带走服务”）；接入真实扫谱/自动调束后建议改为 False，
服务常驻由任务计划程序或 NSSM 管理（见 deploy/README.md）。

开发/测试可用环境变量 ``SPECTRUM_INSTRUMENT_EXE`` 覆盖服务 exe 路径。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QSettings

_DEFAULT_INSTRUMENT_URL = "http://127.0.0.1:8765"
_HEALTH_PATH = "/control/v1/health/live"
_START_TIMEOUT_S = 20.0
_POLL_INTERVAL_S = 0.25

_QSETTINGS_ORG = "SpectrumPlatform"
_QSETTINGS_APP = "DesktopClient"
_INSTRUMENT_URL_KEY = "service/instrumentUrl"
_STOP_ON_EXIT_KEY = "instrument/stopServiceOnExit"


def find_sibling_service_exe(client_exe_dir: Path) -> Path | None:
    """在客户端分发目录旁查找 instrument-service 可执行文件。

    优先支持“操作电脑单文件夹交付”布局：
        <交付根>/spectrum-client/...            （本客户端）
        <交付根>/spectrum-instrument-service/…  （服务，旁挂）
    也兼容服务目录直接内嵌在客户端目录下的布局。
    """
    candidates = (
        client_exe_dir.parent / "spectrum-instrument-service" / "spectrum-instrument-service.exe",
        client_exe_dir / "spectrum-instrument-service" / "spectrum-instrument-service.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class InstrumentServiceSupervisor:
    """探测、拉起并（可选）善后本机仪器执行服务。"""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._started_by_us = False

    @staticmethod
    def service_alive(service_url: str) -> bool:
        """执行服务健康检查是否可达（0.6s 内无响应视为不可达）。"""
        try:
            with urllib.request.urlopen(
                service_url.rstrip("/") + _HEALTH_PATH, timeout=0.6
            ) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001  连接类错误统一按不可达处理
            return False

    def _service_exe(self) -> Path | None:
        override = os.environ.get("SPECTRUM_INSTRUMENT_EXE")
        if override:
            return Path(override)
        if getattr(sys, "frozen", False):
            return find_sibling_service_exe(Path(sys.executable).resolve().parent)
        return None

    def ensure_started(self, service_url: str | None = None) -> bool:
        """确保本机执行服务可达。

        返回 True 表示该服务由本客户端拉起并负责善后；返回 False 表示服务
        原本就在运行、或本机没有可拉起的服务（检索电脑/开发环境）。
        """
        if service_url is None:
            settings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
            service_url = str(
                settings.value(_INSTRUMENT_URL_KEY, _DEFAULT_INSTRUMENT_URL)
            )
        if self.service_alive(service_url):
            return False

        exe = self._service_exe()
        if exe is None:
            return False
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._process = subprocess.Popen([str(exe)], creationflags=flags)
        except OSError:
            return False
        self._started_by_us = True

        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.service_alive(service_url):
                return True
            if self._process.poll() is not None:
                # 服务进程提前退出：端口被占、启动失败等，交给上层状态展示处理。
                break
            time.sleep(_POLL_INTERVAL_S)
        return self.service_alive(service_url)

    def shutdown(self) -> None:
        """按 ``instrument/stopServiceOnExit`` 策略停止由本客户端拉起的服务。"""
        if not self._started_by_us or self._process is None:
            return
        settings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
        stop_on_exit = bool(settings.value(_STOP_ON_EXIT_KEY, True, type=bool))
        if not stop_on_exit:
            # 服务转交系统继续运行（后续由 NSSM/任务计划管理），客户端不再持有。
            self._detach()
            return
        try:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        except OSError:
            pass
        self._detach()

    def _detach(self) -> None:
        self._process = None
        self._started_by_us = False
