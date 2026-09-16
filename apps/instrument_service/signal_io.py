"""受控信号读写的执行层：**硬件写入的唯一通道**。

架构依据（架构文档 6.2 / 6.6 / 9.4）：

* 界面不直连 IOC，写值一律经本模块，由执行层独立完成校验后下发；
* 校验顺序按文档「边界 → 最大单步 → 变化速率」执行，任一不过即拒绝，设备不被改动；
* 写入完成不等于设备稳定（9.2），因此结果同时记录「请求值 / 实际下发值 / 回读值」；
* 下发命令的响应丢失会形成「是否已执行未知」，所以同一 ``command_id`` 重放返回
  首次结果而不重复写设备（9.3：不自动重复可能产生副作用的命令）。

稳定等待（settle_tol / settle_timeout）在此只提供 ``wait_settled`` 原语，
真正的逐点采集时序由扫描/调束状态机决定。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from math import ceil, isfinite
from threading import RLock
from uuid import UUID, uuid4

from packages.contracts import (
    PvMappingConfig,
    PvMappingEntry,
    SignalReading,
    SignalSnapshot,
    SignalWriteRequest,
    SignalWriteResult,
)
from packages.epics_adapter import EpicsGateway

# 斜坡分步上限：超出即拒绝而不是放宽 max_step（宁可拒绝也不静默违反安全约束）。
MAX_RAMP_STEPS = 128
# 幂等缓存容量：只保留最近若干条 command_id，避免无界增长。
_IDEMPOTENCY_CACHE = 256


class WriteRejected(RuntimeError):
    """写入被执行层拒绝。调用方应把 message 原样展示给操作员。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SignalWriteService:
    """按映射配置读写受控信号，并对写入施加执行层校验。"""

    def __init__(
        self,
        gateway: EpicsGateway,
        config: PvMappingConfig,
        *,
        sleep=time.sleep,
        read_only: bool = False,
    ) -> None:
        self._gateway = gateway
        self._config = config
        self._sleep = sleep
        # 全局只读部署模式（改造报告 §4.2）：由部署参数启用，**在执行层统一拒绝所有写入**。
        # 放在这里是刻意的——所有写路径（单点、成组、成组回落、扫谱斜坡、调束下发）
        # 都经过 write()，一个检查点就能覆盖全平台，不会漏掉某条新加的写路径。
        self._read_only = bool(read_only)
        self._lock = RLock()
        # ChannelAccessGateway 的 read/write 不会自己建连：没先 connect() 时
        # 读会返回「网关尚未连接」。服务里原本只有健康检查会 connect，
        # 于是「没跑过健康检查就直接读」必然失败——扫描和调束都会在第 1 步报错。
        # 这里自己做一次惰性建连，并在读数掉线时清标记以便下次重连。
        self._connect_ok = False
        self._entries: dict[str, PvMappingEntry] = {
            entry.signal: entry for entry in config.entries
        }
        # command_id -> 首次写入结果，用于重放幂等
        self._completed: dict[str, SignalWriteResult] = {}

    # ------------------------------------------------------------------
    # 映射查询
    # ------------------------------------------------------------------
    @property
    def entries(self) -> dict[str, PvMappingEntry]:
        return dict(self._entries)

    def entry(self, signal: str) -> PvMappingEntry:
        try:
            return self._entries[signal]
        except KeyError as exc:
            raise WriteRejected(f"未配置的业务信号：{signal}") from exc

    def cached_write_result(self, command_id: str | None) -> SignalWriteResult | None:
        """返回已经成功完成的幂等写结果；查询本身不会触碰设备。"""
        if not command_id:
            return None
        with self._lock:
            return self._completed.get(command_id)

    def resolve(self, signals: list[str] | None = None) -> list[PvMappingEntry]:
        """把请求的信号名列表解析成条目；None/空表示全部。"""
        if not signals:
            return list(self._config.entries)
        return [self.entry(signal) for signal in signals]

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read_snapshot(self, signals: list[str] | None = None) -> SignalSnapshot:
        """批量读取。单个信号失败只标记该项未连接，不影响其余读数。"""
        readings = [self._read_entry(entry) for entry in self.resolve(signals)]
        return SignalSnapshot(taken_at=_now(), readings=readings)

    def _ensure_connected(self) -> None:
        """惰性建连；连不上不抛异常，由调用方按「未连接」如实呈现。"""
        if self._connect_ok:
            return
        try:
            self._connect_ok = bool(self._gateway.connect())
        except Exception:  # noqa: BLE001  连接失败不是异常路径，是「设备不可用」
            self._connect_ok = False

    def _read_entry(self, entry: PvMappingEntry) -> SignalReading:
        self._ensure_connected()
        try:
            reading = self._gateway.read(entry.signal)
        except Exception as exc:  # noqa: BLE001  读取失败转为逐项明细，不打断批量
            return SignalReading(
                signal=entry.signal,
                pv=entry.pv,
                value=None,
                unit=entry.unit,
                connected=False,
                writable=entry.writable,
                detail=str(exc) or type(exc).__name__,
            )
        if not reading.connected:
            # 连接可能掉了：清掉标记，让下一次操作重新建连
            self._connect_ok = False
        return SignalReading(
            signal=entry.signal,
            pv=entry.pv,
            value=float(reading.value),
            unit=entry.unit,
            connected=reading.connected,
            writable=entry.writable,
            severity=reading.severity,
            source_time=reading.source_time.isoformat(),
            received_time=reading.received_time.isoformat(),
            detail=None if reading.connected else "PV 未连接",
        )

    def read_value(self, signal: str) -> float | None:
        """读单个信号的当前值；读不到返回 None（不抛）。"""
        return self._read_signal(signal)

    def _read_signal(self, signal: str) -> float | None:
        """读一路现场值，并**确保连接已建立**。

        必须走 ``_read_entry``：它内部会 ``_ensure_connected()``。真实 CA 网关在
        冷启动时第一次访问要建连，绕过它的直读会**必然失败一次**——调束取启动前
        快照时就会被误判成"读不到"并拒绝启动（实测踩到过：紧接着手工读同一路是好的）。
        """
        try:
            return self._read_entry(self.entry(signal)).value
        except Exception:  # noqa: BLE001  单路读失败按「读不到」处理，不打断调用方
            return None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def write(self, request: SignalWriteRequest) -> SignalWriteResult:
        """校验并执行一次写入。

        同 ``command_id`` 的重复请求返回首次结果，不重复下发设备写命令。
        """
        entry = self.entry(request.signal)

        if self._read_only:
            # 拒绝在**校验之前**：只读部署下连"这个值是否合法"也不必回答，
            # 免得调用方以为"参数对了就能写"。
            return SignalWriteResult(
                signal=entry.signal,
                pv=entry.pv,
                unit=entry.unit,
                requested=request.value,
                accepted=False,
                previous=None,
                ramp_steps=[],
                reason=(
                    "全局只读模式：本执行服务按部署参数禁用了所有写入"
                    "（去掉只读参数并重启服务后才能下发）"
                ),
                device_state_unknown=False,
                finished_at=_now(),
            )

        if request.command_id:
            cached = self.cached_write_result(request.command_id)
            if cached is not None:
                return cached

        result = self._execute(entry, request)

        if request.command_id and result.accepted:
            with self._lock:
                self._completed[request.command_id] = result
                while len(self._completed) > _IDEMPOTENCY_CACHE:
                    self._completed.pop(next(iter(self._completed)))
        return result

    def _execute(
        self, entry: PvMappingEntry, request: SignalWriteRequest
    ) -> SignalWriteResult:
        base = {
            "signal": entry.signal,
            "pv": entry.pv,
            "unit": entry.unit,
            "requested": request.value,
            "command_id": request.command_id,
            "dry_run": request.dry_run,
        }

        def reject(
            reason: str,
            previous: float | None = None,
            *,
            state_unknown: bool = False,
        ) -> SignalWriteResult:
            return SignalWriteResult(
                **base,
                accepted=False,
                previous=previous,
                ramp_steps=[],
                reason=reason,
                device_state_unknown=state_unknown,
                finished_at=_now(),
            )

        # 1) 只读信号与非法数值
        if not entry.writable:
            return reject(f"{entry.label} 配置为只读信号，不允许写入")
        if not isfinite(request.value):
            return reject("写入值不是有限数")

        # 2) 边界检查（文档 6.6 第一步）
        if entry.min_value is not None and request.value < entry.min_value:
            return reject(
                f"低于下限：请求 {request.value:g} < 允许最小 {entry.min_value:g} {entry.unit}"
            )
        if entry.max_value is not None and request.value > entry.max_value:
            return reject(
                f"超过上限：请求 {request.value:g} > 允许最大 {entry.max_value:g} {entry.unit}"
            )

        # 3) 读当前值——单步/速率校验必须基于设备真实当前值，不能基于界面缓存
        self._ensure_connected()
        current = self._read_entry(entry)
        previous = current.value
        if previous is None:
            reason = "当前值读取失败，无法校验变化量（设备状态未知，未写入）"
            if current.detail:
                # 把底层原因带出来：只报「读取失败」在现场无从下手
                reason += f"：{current.detail}"
            return reject(reason)

        delta = request.value - previous
        steps = self._plan_ramp(entry, previous, request.value, delta, request.ramp)
        if isinstance(steps, str):  # 规划失败，steps 是拒绝原因
            return reject(steps, previous=previous)

        if request.dry_run:
            return SignalWriteResult(
                **base,
                accepted=True,
                previous=previous,
                applied=None,
                ramp_steps=steps,
                reason=None,
                finished_at=_now(),
            )

        command = UUID(request.command_id) if request.command_id else uuid4()
        # 步间隔只由「每步变化量 / max_rate」决定，循环外算一次即可；
        # 等待只发生在步之间（首步前与末步后都不等），否则会白白多等一个间隔。
        delay = self._step_delay(entry, steps) if len(steps) > 1 else 0.0
        try:
            for index, value in enumerate(steps):
                if index and delay:
                    self._sleep(delay)
                self._gateway.write(entry.signal, value, command)
        except Exception as exc:  # noqa: BLE001  写入失败如实返回，绝不假装成功
            # 下发中报错意味着「是否已执行未知」：可能已经写到设备了。
            # 调用方必须按结果未知处理，不能当成「设备没动」。
            return reject(
                f"写入设备失败：{exc}", previous=previous, state_unknown=True
            )

        readback = self.readback(entry)
        return SignalWriteResult(
            **base,
            accepted=True,
            previous=previous,
            applied=steps[-1],
            readback=readback,
            ramp_steps=steps,
            reason=None,
            finished_at=_now(),
        )

    def _plan_ramp(
        self,
        entry: PvMappingEntry,
        previous: float,
        target: float,
        delta: float,
        ramp: bool,
    ) -> list[float] | str:
        """规划写入序列；返回步值列表，或返回字符串表示拒绝原因。

        ``ramp=False`` 时不允许把一次大变化拆成多步，超单步直接拒绝——
        调用方要的是「要么一步到位、要么别动设备」的语义。
        """
        max_step = entry.max_step
        if max_step is None or max_step <= 0 or abs(delta) <= max_step:
            return [target]

        if not ramp:
            return (
                f"变化量 {delta:+g} 超过最大单步 {max_step:g} {entry.unit}"
                f"（本次请求未启用斜坡）"
            )

        count = int(ceil(abs(delta) / max_step))
        if count > MAX_RAMP_STEPS:
            return (
                f"变化量 {delta:+g} 需要 {count} 步斜坡，超过上限 {MAX_RAMP_STEPS} 步；"
                f"请分批写入或调整 max_step/max_rate"
            )
        return [previous + delta * (i + 1) / count for i in range(count)]

    def _step_delay(self, entry: PvMappingEntry, steps: list[float]) -> float:
        """按 max_rate 计算相邻步之间的等待秒数；未配置速率则不限速。"""
        if entry.max_rate is None or entry.max_rate <= 0 or len(steps) < 2:
            return 0.0
        step_size = abs(steps[1] - steps[0])
        return step_size / entry.max_rate

    # ------------------------------------------------------------------
    # 回读与稳定判据
    # ------------------------------------------------------------------
    def readback(self, entry: PvMappingEntry) -> float | None:
        """读回条目对应的实际值（优先 readback_signal，未配置则读自身）。

        经由 ``_read_signal`` 读，带建连保证——直读网关在冷启动时会失败一次。
        """
        return self._read_signal(entry.readback_signal or entry.signal)

    def wait_settled(
        self,
        entry: PvMappingEntry,
        target: float,
        *,
        tol: float | None = None,
        timeout: float | None = None,
        readback_signal: str | None = None,
        poll: float = 0.2,
    ) -> tuple[bool, float | None]:
        """等待回读进入 ``target ± tol``；返回 (是否稳定, 最后回读值)。

        **稳定判据始终取自 ``entry``**：它归属在设定条目上（``settle_tol`` /
        ``settle_timeout``），因为「这个设定值要等多久才算到位」是设备属性，
        不是回读信号的属性。``readback_signal`` 只决定用哪一路回读来判定，
        默认用条目自己的 ``readback_signal``。

        传错条目（例如只读的回读条目上没有容差）会得到 ``tolerance is None``，
        从而**直接判定为已稳定**——这正是把未稳定点混进谱图的常见原因，
        所以调用方应当传设定条目。

        超时不算异常：调用方据返回值决定继续还是判定失败
        （文档 9.2：稳定判据按设备定义，不假设写完成即稳定）。
        """
        tolerance = entry.settle_tol if tol is None else tol
        limit = entry.settle_timeout if timeout is None else timeout
        source = readback_signal or entry.readback_signal or entry.signal

        def current() -> float | None:
            # 同样经由 _read_signal：等稳定这条路径在冷启动时也必须是准的
            return self._read_signal(source)

        if tolerance is None:
            return True, current()

        deadline = None if limit is None else time.monotonic() + limit
        last: float | None = None
        while True:
            last = current()
            if last is not None and abs(last - target) <= tolerance:
                return True, last
            if deadline is not None and time.monotonic() >= deadline:
                return False, last
            self._sleep(poll)
