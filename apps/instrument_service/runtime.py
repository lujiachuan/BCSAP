"""执行服务运行时：持有当前 PV 映射配置与网关，支持配置热更新。"""

from __future__ import annotations

import os
from threading import RLock
from uuid import uuid4

from packages.contracts import (
    PvHealthResponse,
    PvMappingConfig,
    SignalBatchWriteResponse,
    SignalWriteRequest,
)

from . import pv_mapping
from .device_locks import DeviceBusy, DeviceLockManager
from .pv_health import check_pv_health, create_gateway
from .retract import RetractOutcome, retract_signals
from .scan_service import ScanService
from .scan_store import ScanStore
from .signal_io import SignalWriteService, WriteRejected
from .tuning_service import TuningService
from .tuning_store import TuningStore

# 全局只读部署模式的开关（环境变量）：**默认关闭**，需要显式打开。
# 用环境变量而不是配置文件：它表达的是"这台机器部署成只读"，属于启动方式的一部分，
# 改动它必然伴随一次重启——比在配置文件里改一个能被 API 覆盖的字段更可靠。
READ_ONLY_ENV = "SPECTRUM_READ_ONLY"
_TRUTHY = {"1", "true", "yes", "on"}


def read_only_from_env() -> bool:
    """读环境变量判断是否启用全局只读模式。"""
    return str(os.environ.get(READ_ONLY_ENV, "")).strip().lower() in _TRUTHY


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
        read_only: bool | None = None,
    ) -> None:
        """``gateway_factory`` 可注入：测试用 ``create_simulated_gateway`` 建内存网关，
        生产保持默认的真实 Channel Access。配置热更新时沿用同一工厂。

        ``store`` 可注入：测试用临时目录，避免往用户暂存目录写测试数据。

        ``read_only`` 缺省取环境变量 ``SPECTRUM_READ_ONLY``（部署参数）：一台只做
        监视/分析的机器上，把整个执行服务设成只读比逐条把信号标成不可写可靠得多。
        """
        self._lock = RLock()
        self._config = config if config is not None else pv_mapping.load_config()
        self._read_only = (
            read_only_from_env() if read_only is None else bool(read_only)
        )
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
    def read_only(self) -> bool:
        """全局只读部署模式是否启用。"""
        return self._read_only

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

    def write_batch(
        self,
        requests: list[SignalWriteRequest],
        *,
        atomic: bool = True,
        note: str = "",
    ) -> SignalBatchWriteResponse:
        """成组写入：批量校验 → 批量锁定 → 逐路下发 → 逐路明细。

        原子校验（``atomic=True``）用执行层的**干跑**完成：边界、单步、速率、
        可写性、当前值可读性全都按正式写入的同一套规则过一遍，任何一项不过就
        **整批不下发**——"前两台已经动了"这种中间状态比整体不动更难收拾。

        锁定是为了不让扫谱/调束在这批写入中间插进来（它们会先抢同一组设备）。
        执行途中的失败如实逐路返回：谁写成功、谁没写、谁的状态未知，一目了然。
        """
        if not requests:
            return SignalBatchWriteResponse(
                ok=False, message="成组写入至少要有一路信号", requested=0,
                accepted=0, rejected=0, nothing_written=True,
            )

        problems: list[str] = []
        for request in requests:
            try:
                entry = self.signals.entry(request.signal)
            except WriteRejected as exc:
                problems.append(str(exc))
                continue
            preview = self.signals.write(
                SignalWriteRequest(
                    signal=request.signal, value=request.value, ramp=request.ramp,
                    dry_run=True,
                )
            )
            if not preview.accepted:
                problems.append(f"{entry.label}：{preview.reason}")
        if atomic and problems:
            return SignalBatchWriteResponse(
                ok=False,
                message=(
                    f"成组写入被拒绝，整批未下发（{len(problems)} 项不合格）："
                    + "；".join(problems)
                ),
                requested=len(requests),
                accepted=0,
                rejected=len(problems),
                nothing_written=True,
            )

        groups = {
            entry.group
            for entry in (
                self.signals.entry(request.signal) for request in requests
            )
            if entry.group
        }
        owner = f"batch-{uuid4()}"
        try:
            self._locks.acquire(groups, owner=owner)
        except DeviceBusy as exc:
            return SignalBatchWriteResponse(
                ok=False,
                message=f"设备组当前不可用，成组写入未执行：{exc}",
                requested=len(requests),
                accepted=0,
                rejected=len(requests),
                nothing_written=True,
                locked_groups=sorted(self._locks.held()),
            )
        try:
            results = [self.signals.write(request) for request in requests]
        finally:
            self._locks.release(owner=owner)

        accepted = sum(1 for result in results if result.accepted)
        rejected = len(results) - accepted
        unknown = [result.signal for result in results if result.device_state_unknown]
        if rejected == 0:
            message = f"成组写入完成（{accepted} 路）" + (f"：{note}" if note else "")
        else:
            failed = "、".join(
                f"{result.signal}（{result.reason or '未说明'}）"
                for result in results
                if not result.accepted
            )
            message = (
                f"成组写入部分成功：{accepted} 路已下发、{rejected} 路失败——{failed}"
            )
            if unknown:
                message += f"；其中 {'、'.join(unknown)} 的设备状态未知，需人工确认"
        return SignalBatchWriteResponse(
            ok=rejected == 0,
            message=message,
            requested=len(results),
            accepted=accepted,
            rejected=rejected,
            results=results,
            locked_groups=sorted(self._locks.held()),
        )

    def retract_magnets(
        self,
        setpoint_signals: list[str],
        current_a: float,
        rate_a_s: float | None,
        timeout_s: float | None = None,
    ) -> RetractOutcome:
        """手动成组回落：与扫描收尾走**同一套**服务端逻辑。

        设备组锁由这里自己声明（``owner`` 是一次性 id）：扫谱/调束正在占用磁铁组时
        会被 ``DeviceBusy`` 挡住，而不是在别人的任务下面偷偷写设定值。
        """
        groups = {
            entry.group
            for entry in (
                self.signals.entry(signal) for signal in setpoint_signals
            )
            if entry.group
        }
        owner = f"retract-{uuid4()}"
        self._locks.acquire(groups, owner=owner)
        try:
            return retract_signals(
                self.signals, setpoint_signals, current_a, rate_a_s, timeout_s=timeout_s
            )
        finally:
            self._locks.release(owner=owner)

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
                self._signals = SignalWriteService(
                    self.gateway(), self._config, read_only=self._read_only
                )
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
