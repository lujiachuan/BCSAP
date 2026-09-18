"""使用现场已有 caget/caput 的 EPICS 命令行网关。"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import UUID

from .base import Reading


class CommandLineEpicsGateway:
    """每次读写启动独立 caget/caput 进程，不持有 CA 长连接。"""

    # 批量 caget 遇到一条离线 PV 时，部分版本不会输出其它成功值。二分补探可以
    # 隔离坏路，但必须同时限制进程数，避免全网离线时展开成 2N-1 个子进程。
    MAX_SNAPSHOT_COMMANDS = 32
    SNAPSHOT_CACHE_S = 0.25
    OFFLINE_RETRY_S = 10.0

    def __init__(
        self,
        paths: dict[str, str],
        units: dict[str, str] | None = None,
        *,
        caget_path: str | None = None,
        caput_path: str | None = None,
        timeout: float = 3.0,
        batch_timeout: float = 20.0,
        runner=subprocess.run,
        clock=monotonic,
    ) -> None:
        self._paths = dict(paths)
        self._units = dict(units or {})
        self._caget = caget_path or _find_epics_command("caget")
        self._caput = caput_path or _find_epics_command("caput")
        self._timeout = timeout
        self._batch_timeout = batch_timeout
        self._runner = runner
        self._clock = clock
        self._snapshot_lock = RLock()
        self._snapshot_cache: dict[str, Reading] = {}
        self._snapshot_times: dict[str, float] = {}
        self._offline_until: dict[str, float] = {}
        self._offline_details: dict[str, str] = {}

    def connect(self) -> bool:
        if not self._caget:
            raise ConnectionError("找不到 caget.exe；请把 EPICS 命令行工具目录加入系统 PATH")
        return True

    def read(self, signal: str) -> Reading:
        pv = self._pv(signal)
        output, _detail = self._run([self._caget, "-t", "-n", pv])
        try:
            value = float(output.splitlines()[-1].strip())
        except (IndexError, ValueError) as exc:
            raise ConnectionError(f"caget 返回了无法识别的值（{pv}）：{output}") from exc
        return self._reading(signal, value)

    def write(self, signal: str, value: float, command_id: UUID) -> Reading:
        del command_id
        pv = self._pv(signal)
        if not self._caput:
            raise ConnectionError("找不到 caput.exe；当前只能读取 PV，不能下发控制值")
        with self._snapshot_lock:
            self._run([self._caput, "-t", pv, format(float(value), ".17g")])
            self._snapshot_cache.clear()
            self._snapshot_times.clear()
            self._offline_until.pop(signal, None)
            self._offline_details.pop(signal, None)
        return self._reading(signal, float(value))

    def snapshot(self, signals: list[str]) -> dict[str, Reading]:
        if not signals:
            return {}
        self.connect()
        with self._snapshot_lock:
            return self._snapshot_locked(signals)

    def _snapshot_locked(self, signals: list[str]) -> dict[str, Reading]:
        now = self._clock()
        if all(
            signal in self._snapshot_cache
            and now - self._snapshot_times.get(signal, 0.0) <= self.SNAPSHOT_CACHE_S
            for signal in signals
        ):
            return {signal: self._snapshot_cache[signal] for signal in signals}

        active = [
            signal for signal in signals if signal not in self._offline_until
        ]
        retry = [
            signal for signal in signals
            if 0.0 < self._offline_until.get(signal, 0.0) <= now
        ]
        values: dict[str, float] = {}
        details: dict[str, str] = {}
        deadline = self._clock() + self._batch_timeout
        used = 0

        if active:
            found, errors, used = self._probe_groups(
                active, deadline, self.MAX_SNAPSHOT_COMMANDS
            )
            values.update(found)
            details.update(errors)
        if retry and used < self.MAX_SNAPSHOT_COMMANDS:
            found, errors, retry_used = self._probe_groups(
                retry, deadline, self.MAX_SNAPSHOT_COMMANDS - used
            )
            values.update(found)
            details.update(errors)
            used += retry_used

        finished = self._clock()
        readings: dict[str, Reading] = {}
        for signal in signals:
            pv = self._pv(signal)
            connected = pv in values
            if connected:
                self._offline_until.pop(signal, None)
                self._offline_details.pop(signal, None)
                detail = None
            elif signal in active or signal in retry:
                detail = details.get(signal) or "caget 未返回该 PV"
                self._offline_until[signal] = finished + self.OFFLINE_RETRY_S
                self._offline_details[signal] = detail
            else:
                detail = self._offline_details.get(signal) or "PV 暂时离线，等待重试"
            reading = self._reading(
                signal,
                values.get(pv, 0.0),
                connected=connected,
                detail=detail,
            )
            readings[signal] = reading
            self._snapshot_cache[signal] = reading
            self._snapshot_times[signal] = finished
        return readings

    def _probe_groups(
        self,
        signals: list[str],
        deadline: float,
        command_limit: int,
    ) -> tuple[dict[str, float], dict[str, str], int]:
        values: dict[str, float] = {}
        details: dict[str, str] = {}
        pending = deque([list(signals)])
        command_count = 0

        while pending and command_count < command_limit:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            group = pending.popleft()
            try:
                output, error = self._run(
                    [self._caget, "-n", *(self._pv(signal) for signal in group)],
                    allow_nonzero=True,
                    timeout=min(self._timeout, remaining),
                )
            except ConnectionError as exc:
                output, error = "", str(exc)
            command_count += 1
            found = self._parse_batch(output)
            values.update(found)
            for signal in group:
                if self._pv(signal) in found:
                    details.pop(signal, None)
            missing = [signal for signal in group if self._pv(signal) not in found]
            if not missing:
                continue
            detail = error or "caget 未返回该 PV"
            for signal in missing:
                details[signal] = detail
            if len(missing) > 1:
                middle = len(missing) // 2
                pending.append(missing[:middle])
                pending.append(missing[middle:])

        if pending:
            detail = (
                f"caget 补探达到限制（最多 {command_limit} 个进程、"
                f"总计 {self._batch_timeout:g} 秒）"
            )
            for group in pending:
                for signal in group:
                    details[signal] = detail

        return values, details, command_count

    def close(self) -> None:
        return None

    def _pv(self, signal: str) -> str:
        try:
            return self._paths[signal]
        except KeyError as exc:
            raise KeyError(f"未配置业务信号：{signal}") from exc

    def _run(
        self,
        args: list[str],
        *,
        allow_nonzero: bool = False,
        timeout: float | None = None,
    ) -> tuple[str, str | None]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            completed = self._runner(
                args,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=self._timeout if timeout is None else timeout,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConnectionError(
                f"EPICS 命令执行超时（{float(exc.timeout):g} 秒）"
            ) from exc
        except OSError as exc:
            raise ConnectionError(f"EPICS 命令执行失败：{exc}") from exc
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        if completed.returncode != 0 and not allow_nonzero:
            raise ConnectionError(f"EPICS 命令返回 {completed.returncode}：{detail}")
        return (
            completed.stdout.strip(),
            f"EPICS 命令返回 {completed.returncode}：{detail}"
            if completed.returncode != 0
            else None,
        )

    @staticmethod
    def _parse_batch(output: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                values[parts[0]] = float(parts[1].split()[0])
            except ValueError:
                continue
        return values

    def _reading(
        self,
        signal: str,
        value: float,
        *,
        connected: bool = True,
        detail: str | None = None,
    ) -> Reading:
        now = datetime.now(UTC)
        return Reading(
            signal=signal,
            value=value,
            unit=self._units.get(signal, ""),
            source_time=now,
            received_time=now,
            connected=connected,
            detail=detail,
        )


def _find_epics_command(name: str) -> str | None:
    """从 PATH、EPICS_BASE 和打包服务同目录查找 EPICS 命令。"""
    executable = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    if getattr(sys, "frozen", False):
        service_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            (service_dir / executable, service_dir / "epics" / executable)
        )
    base = os.environ.get("EPICS_BASE")
    if base:
        bin_dir = Path(base) / "bin"
        arch = os.environ.get("EPICS_HOST_ARCH")
        if arch:
            candidates.append(bin_dir / arch / executable)
        candidates.append(bin_dir / executable)
        if bin_dir.is_dir():
            candidates.extend(sorted(bin_dir.glob(f"*/{executable}")))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class FailoverEpicsGateway:
    """主 CA 网关初始化失败时切换到命令行网关。"""

    def __init__(self, primary, fallback, probe_signal: str | None = None) -> None:
        self._primary = primary
        self._fallback = fallback
        self._probe_signal = probe_signal
        self._active = None

    def connect(self) -> bool:
        if self._active is not None:
            return True
        primary_connected = False
        try:
            primary_connected = bool(self._primary.connect())
            if not primary_connected:
                raise ConnectionError("CA DLL 通道初始化返回失败")
            if self._probe_signal:
                probe = self._primary.read(self._probe_signal)
                if not probe.connected:
                    raise ConnectionError("CA DLL 已加载，但探测 PV 未连接")
            self._active = self._primary
            return True
        except Exception as primary_error:  # noqa: BLE001  回退边界需要保留原始原因
            try:
                connected = bool(self._fallback.connect())
                if self._probe_signal:
                    self._fallback.read(self._probe_signal)
            except Exception as fallback_error:  # noqa: BLE001
                if primary_connected:
                    # 首路 PV 可能恰好离线；两条通道都读不到时仍保留主通道，
                    # 后续健康检查会逐项判断，不能把其余 PV 一起误报为不可用。
                    self._active = self._primary
                    return True
                raise ConnectionError(
                    f"CA DLL 通道不可用：{primary_error}；"
                    f"caget/caput 后备通道也不可用：{fallback_error}"
                ) from fallback_error
            self._active = self._fallback
            return connected

    def read(self, signal: str) -> Reading:
        self.connect()
        return self._active.read(signal)

    def write(self, signal: str, value: float, command_id: UUID) -> Reading:
        self.connect()
        return self._active.write(signal, value, command_id)

    def snapshot(self, signals: list[str]) -> dict[str, Reading]:
        self.connect()
        return self._active.snapshot(signals)

    def close(self) -> None:
        for gateway in (self._primary, self._fallback):
            close = getattr(gateway, "close", None)
            if callable(close):
                close()
