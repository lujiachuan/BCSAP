"""执行服务运行时：持有当前 PV 映射配置与网关，支持配置热更新。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from packages.contracts import (
    PvHealthResponse,
    PvMappingConfig,
    SignalBatchWriteResponse,
    SignalWriteRequest,
    SignalWriteResult,
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
        self._config_error = ""
        if config is not None:
            self._config = config
        else:
            try:
                self._config = pv_mapping.load_config_checked()
            except pv_mapping.PvMappingLoadError as exc:
                self._config = pv_mapping.default_config()
                self._config_error = str(exc)
        self._deployment_read_only = (
            read_only_from_env() if read_only is None else bool(read_only)
        )
        self._read_only = bool(self._config_error) or self._deployment_read_only
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
        self._recovery_checked = False
        self._startup_recoveries: dict[str, dict] = {}

    @property
    def config(self) -> PvMappingConfig:
        with self._lock:
            return self._config

    @property
    def read_only(self) -> bool:
        """部署只读或配置损坏保护是否正在阻止设备写入。"""
        return self._read_only

    @property
    def deployment_read_only(self) -> bool:
        return self._deployment_read_only

    @property
    def config_error(self) -> str:
        return self._config_error

    def write_signal(self, request: SignalWriteRequest) -> SignalWriteResult:
        """单路手动写入也必须短暂占用设备组，不能穿透扫谱/调束锁。"""
        entry = self.signals.entry(request.signal)
        cached = self.signals.cached_write_result(request.command_id)
        if cached is not None:
            return cached
        owner = f"manual-{uuid4()}"
        try:
            self._locks.acquire([entry.group or "__ungrouped__"], owner=owner)
        except DeviceBusy as exc:
            return SignalWriteResult(
                signal=entry.signal,
                pv=entry.pv,
                unit=entry.unit,
                accepted=False,
                requested=request.value,
                reason=str(exc),
                command_id=request.command_id,
                dry_run=request.dry_run,
                finished_at=datetime.now(UTC).isoformat(),
            )
        try:
            return self.signals.write(request)
        finally:
            self._locks.release(owner=owner)

    def mapping_update_blocker(self) -> str | None:
        held = self._locks.held()
        with self._lock:
            scan = self._scan
            tuning = self._tuning
        scan_id = scan.active_run_id() if scan is not None else None
        tuning_id = tuning.active_run_id() if tuning is not None else None
        if not held and scan_id is None and tuning_id is None:
            return None
        detail = "、".join(f"{group}（{owner}）" for group, owner in held.items())
        tasks = "、".join(
            text
            for text in (
                f"扫谱 {scan_id}" if scan_id else "",
                f"调束 {tuning_id}" if tuning_id else "",
            )
            if text
        )
        reason = "；".join(part for part in (tasks, detail) if part)
        return f"存在活动任务或待恢复设备组，暂不能修改映射：{reason}"

    def acquire_mapping_update(self, config: PvMappingConfig) -> str:
        """在保存到切换完成期间锁住全部旧/新设备组，封闭检查后的竞态窗口。"""
        blocker = self.mapping_update_blocker()
        if blocker:
            raise DeviceBusy(blocker)
        groups = {
            entry.group or "__ungrouped__"
            for entry in (*self.config.entries, *config.entries)
        }
        owner = f"mapping-{uuid4()}"
        self._locks.acquire(groups, owner=owner)
        return owner

    def release_mapping_update(self, owner: str) -> None:
        self._locks.release(owner=owner)

    @property
    def startup_recoveries(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._startup_recoveries.values()]

    def recover_startup(self) -> None:
        """把上次进程遗留的非终态任务转为恢复屏障并重新占用相关设备组。"""
        with self._lock:
            if self._recovery_checked:
                return
        by_signal = {entry.signal: entry for entry in self.config.entries}
        candidates: list[tuple[str, object, list[str]]] = []
        for row in self.store.incomplete_runs():
            try:
                axis = json.loads(row["axis_json"] or "{}")
                signals = list(axis.get("setpoint_signals") or [])
            except (AttributeError, TypeError, ValueError):
                signals = []
            candidates.append(("scan", row, signals))
        for row in self.tuning_store.incomplete_runs():
            try:
                variables = json.loads(row["variables_json"] or "[]")
                signals = [
                    str(item.get("signal") or "")
                    for item in variables
                    if isinstance(item, dict)
                ]
            except (TypeError, ValueError):
                signals = []
            candidates.append(
                ("tuning", row, signals)
            )
        prepared: list[tuple[str, object, list[str]]] = []
        all_groups: set[str] = set()
        known_groups = {entry.group for entry in self.config.entries if entry.group}
        for kind, row, signals in candidates:
            groups = sorted(
                {
                    by_signal[signal].group
                    for signal in signals
                    if signal in by_signal and by_signal[signal].group
                }
                or known_groups
            )
            prepared.append((kind, row, groups))
            all_groups.update(groups)
        if all_groups:
            self._locks.acquire(all_groups, owner="startup-recovery")
        for kind, row, groups in prepared:
            run_id = str(row["run_id"])
            recovery = {
                "run_id": run_id,
                "kind": kind,
                "groups": groups,
                "message": "服务重启时任务未处于终态，请核对设备实际状态后确认释放",
            }
            self._startup_recoveries[run_id] = recovery
            if kind == "scan":
                self.store.set_state(run_id, "recovery_required")
            else:
                self.tuning_store.set_state(run_id, "recovery_required", recovery["message"])
        with self._lock:
            self._recovery_checked = True

    def acknowledge_startup_recovery(self, run_id: str, note: str = "") -> dict:
        with self._lock:
            try:
                recovery = dict(self._startup_recoveries[run_id])
            except KeyError as exc:
                raise ValueError(f"没有这个启动恢复项：{run_id}") from exc
            if recovery["kind"] == "scan":
                self.store.set_state(run_id, "aborted")
            else:
                message = f"已人工确认：{note}".rstrip("：")
                self.tuning_store.set_state(run_id, "aborted", message)
            self._startup_recoveries.pop(run_id)
            if not self._startup_recoveries:
                self._locks.release(owner="startup-recovery")
        recovery["acknowledged"] = True
        recovery["note"] = note
        return recovery

    def acknowledge_all_startup_recoveries(self, note: str = "") -> list[dict]:
        return [
            self.acknowledge_startup_recovery(run_id, note)
            for run_id in list(self._startup_recoveries)
        ]

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
            entry.group or "__ungrouped__"
            for entry in (
                self.signals.entry(request.signal) for request in requests
            )
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
            entry.group or "__ungrouped__"
            for entry in (
                self.signals.entry(signal) for signal in setpoint_signals
            )
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
        replacement = self._gateway_factory(config)
        with self._lock:
            previous = self._gateway
            self._config = config
            self._config_error = ""
            self._read_only = self._deployment_read_only
            self._gateway = replacement
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
            tuning = self._tuning
        if scan is not None:
            for run_id in scan.run_ids():
                try:
                    scan.stop(run_id)
                except Exception:  # noqa: BLE001  关闭路径尽力而为
                    pass
            scan.wait_idle(10.0)
        if tuning is not None:
            for run_id in tuning.run_ids():
                try:
                    tuning.stop(run_id)
                except Exception:  # noqa: BLE001  关闭路径尽力而为
                    pass
            tuning.wait_idle(10.0)
        self._close_gateway()

    def _close_gateway(self) -> None:
        with self._lock:
            if self._gateway is not None:
                close = getattr(self._gateway, "close", None)
                if callable(close):
                    close()
                self._gateway = None
            self._signals = None
