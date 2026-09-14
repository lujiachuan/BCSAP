"""执行服务运行时：持有当前 PV 映射配置与网关，支持配置热更新。"""

from __future__ import annotations

from threading import RLock

from packages.contracts import PvHealthResponse, PvMappingConfig

from . import pv_mapping
from .device_locks import DeviceLockManager
from .pv_health import check_pv_health, create_gateway
from .scan_service import ScanService
from .scan_store import ScanStore
from .signal_io import SignalWriteService
from .tuning_service import TuningService
from .tuning_store import TuningStore


class InstrumentRuntime:
    """把「当前配置 + 按配置构建的网关」放在一起管理。

    网关按需创建：只有真的要用（健康检查/读写）时才建，避免服务刚起来就
    去连 IOC。配置更新时关闭旧网关并换新，保证映射改动立即生效。

    设备组锁与本地暂存**不随配置重建**：前者表达设备占用，后者是已落盘的数据，
    都与「业务信号怎么映射到 PV」无关。
    """

    def __init__(
        self,
        config: PvMappingConfig | None = None,
        gateway_factory=create_gateway,
        store: ScanStore | None = None,
        tuning_store: TuningStore | None = None,
    ) -> None:
        """``gateway_factory`` 可注入：测试用 ``create_simulated_gateway`` 建内存网关，
        生产保持默认的真实 Channel Access。配置热更新时沿用同一工厂。

        ``store`` 可注入：测试用临时目录，避免往用户暂存目录写测试数据。
        """
        self._lock = RLock()
        self._config = config if config is not None else pv_mapping.load_config()
        self._gateway_factory = gateway_factory
        self._gateway = None
        self._signals: SignalWriteService | None = None
        self._locks = DeviceLockManager()
        # 暂存目录与扫谱服务都**按需创建**：``app.py`` 在模块级就 ``create_app()``，
        # 若在这里落盘建目录，任何一次 ``import app`` 都会去动用户配置目录——
        # 在权限受限的环境里直接让整个模块导入失败。
        self._store = store
        self._scan: ScanService | None = None
        self._tuning_store = tuning_store
        self._tuning: TuningService | None = None

    @property
    def config(self) -> PvMappingConfig:
        with self._lock:
            return self._config

    @property
    def locks(self) -> DeviceLockManager:
        return self._locks

    @property
    def store(self) -> ScanStore:
        """本地暂存（首次使用时才建目录）。"""
        with self._lock:
            if self._store is None:
                self._store = ScanStore()
            return self._store

    @property
    def scan(self) -> ScanService:
        with self._lock:
            if self._scan is None:
                self._scan = ScanService(
                    lambda: self.signals, self._locks, self.store
                )
            return self._scan

    @property
    def tuning_store(self) -> TuningStore:
        """调束记录暂存（首次使用时才建目录）。"""
        with self._lock:
            if self._tuning_store is None:
                self._tuning_store = TuningStore()
            return self._tuning_store

    @property
    def tuning(self) -> TuningService:
        with self._lock:
            if self._tuning is None:
                self._tuning = TuningService(
                    lambda: self.signals, self._locks, self.tuning_store
                )
            return self._tuning

    def gateway(self):
        with self._lock:
            if self._gateway is None:
                self._gateway = self._gateway_factory(self._config)
            return self._gateway

    @property
    def signals(self) -> SignalWriteService:
        """受控信号读写服务（硬件写入的唯一通道）。"""
        with self._lock:
            if self._signals is None:
                self._signals = SignalWriteService(self.gateway(), self._config)
            return self._signals

    def apply(self, config: PvMappingConfig) -> None:
        """热更新配置：换上新网关并关闭旧网关。

        读写服务与网关、配置一一对应，必须一并重建，否则会继续用旧映射写设备。
        """
        with self._lock:
            previous = self._gateway
            self._config = config
            self._gateway = self._gateway_factory(config)
            self._signals = None
            if previous is not None:
                close = getattr(previous, "close", None)
                if callable(close):
                    close()

    def check_health(self) -> PvHealthResponse:
        with self._lock:
            return check_pv_health(self.gateway(), self._config)

    def close(self) -> None:
        """停止在跑的扫谱任务并等线程退出，再关网关。

        顺序不能反：先关网关会让正在收尾的任务拿到一个已关闭的网关，
        写出更难排查的二次错误。
        """
        with self._lock:
            scan = self._scan
        if scan is not None:
            for run_id in scan.run_ids():
                try:
                    scan.stop(run_id)
                except Exception:  # noqa: BLE001  关闭路径尽力而为
                    pass
            scan.wait_idle(10.0)
        self._close_gateway()

    def _close_gateway(self) -> None:
        with self._lock:
            if self._gateway is not None:
                close = getattr(self._gateway, "close", None)
                if callable(close):
                    close()
                self._gateway = None
            self._signals = None
