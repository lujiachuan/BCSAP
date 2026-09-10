"""真实 EPICS Channel Access 网关（ctypes 直连 ca.dll，无第三方依赖）。

为什么用 ctypes 手写：现场机器常常无法访问 PyPI，装不上 pyepics/caproto；
而 EPICS base 自带 ``ca.dll``，Channel Access 的 C API 几十年稳定。这个模块
只依赖标准库 ctypes。

线程模型（实测关键结论）
------------------------
CA 上下文与创建它的线程绑定。实测本机 CA 3.15.6 构建：

* ``ca_context_create(0)``（非抢占式）从其他线程调用 CA 函数会**静默返回
  0.0 且 rc=ECA_NORMAL**（假成功），随后进程可能崩溃；
* 即使换成 ``ca_context_create(1)``（抢占式），跨线程同样返回 0.0。

因此本模块把上下文和**所有** CA 调用都放在一个专用线程里，外部线程只通过
队列提交任务。这样不存在任何跨线程 CA 调用，同时天然串行化硬件访问——
对仪器控制来说是想要的性质。

其它实测结论
------------
* ``ca_pend_io`` 的返回值不能用来判断连接：它可能返回 ECA_NORMAL 而通道尚未
  连上。判定连接一律用 ``ca_state() == cs_conn`` 轮询（配合 ``ca_pend_event``）。
* 连接回调可以不传（NULL）；传回调反而容易踩 CFUNCTYPE 的 ABI 坑。
* ``ca_get``/``ca_put`` 等类型化名字在 ``cadef.h`` 里只是宏，DLL 只导出通用型
  分派的 ``ca_array_get`` / ``ca_array_put``。
* ``dbr_time_double`` 的时间戳是 EPICS 纪元（1990-01-01 UTC），需加偏移换算
  为 Unix 时间。
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

from .base import Reading

# ---- dbr 类型（db_access.h）----
DBR_DOUBLE = 6
DBR_TIME_DOUBLE = 20

# ---- CA 状态码 ----
ECA_NORMAL = 1

# ---- ca_state 返回值（cadef.h）----
CS_NEVER_CONN = 0
CS_PREV_CONN = 1
CS_CONN = 2
CS_CLOSED = 3

# ---- EPICS 纪元（1990-01-01T00:00:00Z）与 Unix 纪元之差 ----
EPICS_EPOCH_OFFSET_S = 631_152_000

_DEFAULT_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_IO_TIMEOUT_S = 2.0

_chid = ctypes.c_void_p


class _EpicsTimeStamp(ctypes.Structure):
    _fields_ = [("secPastEpoch", ctypes.c_uint32), ("nsec", ctypes.c_uint32)]


class _DbrTimeDouble(ctypes.Structure):
    """对应 C 的 dbr_time_double：一次取回 value + severity + 时间戳。"""

    _fields_ = [
        ("status", ctypes.c_short),
        ("severity", ctypes.c_short),
        ("stamp", _EpicsTimeStamp),
        ("value", ctypes.c_double),
    ]


class _Result:
    """跨线程取回调用结果。"""

    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None


def _as_directory(raw: str) -> Path:
    path = Path(raw)
    return path.parent if path.suffix.lower() == ".dll" else path


def ca_library_candidates(configured_dir: str | None = None) -> list[Path]:
    """列出要尝试的 ca.dll 所在目录。

    显式指定（参数或 ``SPECTRUM_CA_LIB_DIR`` / ``EPICS_CA_LIB_DIR`` 环境变量）时
    **只认指定的那一个**：宁可报错，也不静默换用另一个 CA 库——版本不受控的静默
    回退在现场很难排查。

    未指定时才自动发现。实测：EPICS base 的 mingw 构建
    （``bin/windows-x64-mingw``）依赖 ``libgcc_s_seh-1.dll`` / ``libstdc++-6.dll``，
    很多机器上并不存在，加载会失败；而官方 MSVC 构建的 CA 分发只需
    VCRUNTIME140/MSVCP140。所以候选按顺序尝试并汇总失败原因，而不是猜一个。
    """
    explicit = (
        configured_dir
        or os.environ.get("SPECTRUM_CA_LIB_DIR")
        or os.environ.get("EPICS_CA_LIB_DIR")
    )
    if explicit:
        return [_as_directory(explicit)]

    candidates: list[Path] = []

    def add(raw: str | None) -> None:
        if not raw:
            return
        directory = _as_directory(raw)
        if directory not in candidates:
            candidates.append(directory)

    base = os.environ.get("EPICS_BASE")
    arch = os.environ.get("EPICS_HOST_ARCH")
    if base and arch:
        add(str(Path(base) / "bin" / arch))
    if base:
        add(str(Path(base) / "bin"))
    # 同盘旁挂的官方 CA 分发（EPICS 官方 release 目录名形如 CA-3.15.6-windows-x64）
    if base:
        parent = Path(base).parent
        if parent.is_dir():
            for child in sorted(parent.glob("CA-*-windows-*")):
                add(str(child))
            for child in sorted(parent.glob("CA-*")):
                add(str(child))
    return candidates


def load_ca_library(configured_dir: str | None = None) -> tuple[ctypes.CDLL, Path]:
    """加载 ca.dll，返回 (库句柄, 实际目录)。

    显式指定目录时只试该目录；自动发现时按候选顺序尝试。失败时抛出带全部尝试
    细节的 ConnectionError，避免只报一句「找不到模块」让现场无从下手。
    """
    attempts: list[str] = []
    for directory in ca_library_candidates(configured_dir):
        dll = directory / "ca.dll"
        if not dll.is_file():
            attempts.append(f"{directory}：无 ca.dll")
            continue
        try:
            os.add_dll_directory(str(directory))
        except (OSError, AttributeError) as exc:
            attempts.append(f"{directory}：无法注册依赖目录（{exc}）")
            continue
        try:
            library = ctypes.CDLL(str(dll))
        except OSError as exc:
            attempts.append(f"{directory}：{exc}")
            continue
        return library, directory
    detail = "；".join(attempts) if attempts else "未配置任何候选目录"
    raise ConnectionError(
        "无法加载 EPICS Channel Access 库（ca.dll）。已尝试：" + detail
    )


class ChannelAccessGateway:
    """实现 ``EpicsGateway`` 接口的真实 CA 网关。

    ``paths`` 为业务信号到真实 PV 名的映射，``units`` 为信号到工程单位的映射
    （单位取自受控配置，不从 IOC 读取，避免依赖 DBR_CTRL 结构差异）。
    """

    def __init__(
        self,
        paths: dict[str, str],
        units: dict[str, str] | None = None,
        lib_dir: str | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_S,
        io_timeout: float = _DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        self._paths = dict(paths)
        self._units = dict(units or {})
        self._lib_dir = lib_dir
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._channels: dict[str, ctypes.c_void_p] = {}
        self._ca: ctypes.CDLL | None = None
        self._loaded_dir: Path | None = None
        self._connected = False
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="epics-ca", daemon=True
        )
        self._thread.start()

    # ---------- 专用线程 ----------

    def _run(self) -> None:
        try:
            library, directory = load_ca_library(self._lib_dir)
            self._bind(library)
            rc = library.ca_context_create(0)
            if rc != ECA_NORMAL:
                raise ConnectionError(f"ca_context_create 失败，返回 {rc}")
            self._ca = library
            self._loaded_dir = directory
        except BaseException as exc:  # noqa: BLE001  启动期异常统一留给首次调用抛出
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()

        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, result = job
            try:
                result.value = fn(self._ca)
            except BaseException as exc:  # noqa: BLE001  转交调用方
                result.error = exc
            finally:
                result.done.set()

        try:
            for handle in self._channels.values():
                self._ca.ca_clear_channel(handle)
            self._ca.ca_pend_io(self._io_timeout)
            self._ca.ca_context_destroy()
        except Exception:  # noqa: BLE001  退出路径尽力而为
            pass

    def _bind(self, library: ctypes.CDLL) -> None:
        library.ca_context_create.argtypes = [ctypes.c_int]
        library.ca_context_create.restype = ctypes.c_int
        library.ca_context_destroy.argtypes = []
        library.ca_context_destroy.restype = ctypes.c_int
        # 回调传 NULL：连接状态一律用 ca_state 轮询，避免回调 ABI 风险
        library.ca_create_channel.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(_chid),
        ]
        library.ca_create_channel.restype = ctypes.c_int
        library.ca_pend_io.argtypes = [ctypes.c_double]
        library.ca_pend_io.restype = ctypes.c_int
        library.ca_pend_event.argtypes = [ctypes.c_double]
        library.ca_pend_event.restype = ctypes.c_int
        library.ca_flush_io.argtypes = []
        library.ca_flush_io.restype = ctypes.c_int
        library.ca_state.argtypes = [_chid]
        library.ca_state.restype = ctypes.c_int
        library.ca_array_get.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            _chid,
            ctypes.c_void_p,
        ]
        library.ca_array_get.restype = ctypes.c_int
        library.ca_array_put.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            _chid,
            ctypes.c_void_p,
        ]
        library.ca_array_put.restype = ctypes.c_int
        library.ca_read_access.argtypes = [_chid]
        library.ca_read_access.restype = ctypes.c_int
        library.ca_write_access.argtypes = [_chid]
        library.ca_write_access.restype = ctypes.c_int
        library.ca_clear_channel.argtypes = [_chid]
        library.ca_clear_channel.restype = ctypes.c_int
        library.ca_message.argtypes = [ctypes.c_int]
        library.ca_message.restype = ctypes.c_char_p

    def _submit(self, fn, timeout: float):
        if not self._ready.wait(timeout=10.0):
            raise ConnectionError("EPICS CA 线程启动超时")
        if self._startup_error is not None:
            raise self._startup_error
        result = _Result()
        self._jobs.put((fn, result))
        if not result.done.wait(timeout=timeout):
            raise TimeoutError("EPICS CA 调用超时")
        if result.error is not None:
            raise result.error
        return result.value

    # ---------- EpicsGateway ----------

    def connect(self) -> bool:
        """建立通道并等待连接。返回 CA 上下文是否可用（不代表每个 PV 都连上）。

        幂等：已连接时直接返回，避免健康检查每次轮询都重做一遍连接等待。
        连接时缺席的 PV 由 CA 在后台持续搜索，``read()`` 里的 ``ca_pend_event``
        会让它自愈。
        """
        if self._connected:
            return True

        def job(ca) -> bool:
            self._channels.clear()
            for signal, pv in self._paths.items():
                handle = _chid()
                ca.ca_create_channel(pv.encode("utf-8"), None, None, 0, ctypes.byref(handle))
                self._channels[signal] = handle
            ca.ca_flush_io()
            # 关键：不能信 ca_pend_io 的返回值，必须轮询 ca_state
            deadline = monotonic() + self._connect_timeout
            while monotonic() < deadline:
                ca.ca_pend_event(0.05)
                if self._channels and all(
                    ca.ca_state(handle) == CS_CONN for handle in self._channels.values()
                ):
                    break
            self._connected = True
            return True

        return bool(self._submit(job, timeout=self._connect_timeout + 10.0))

    def read(self, signal: str) -> Reading:
        if not self._connected:
            raise ConnectionError("EPICS 网关尚未连接")
        if signal not in self._paths:
            raise KeyError(f"未配置业务信号：{signal}")

        def job(ca) -> Reading:
            handle = self._channels[signal]
            if ca.ca_state(handle) != CS_CONN:
                # 给 CA 一点时间推进后台搜索，IOC 后起时能自愈
                ca.ca_pend_event(0.05)
                if ca.ca_state(handle) != CS_CONN:
                    return self._reading(signal, 0.0, connected=False, severity=None)
            payload = _DbrTimeDouble()
            rc = ca.ca_array_get(
                DBR_TIME_DOUBLE, 1, handle, ctypes.byref(payload)
            )
            ca.ca_pend_io(self._io_timeout)
            if rc != ECA_NORMAL:
                return self._reading(signal, 0.0, connected=False, severity=None)
            return self._reading(
                signal,
                payload.value,
                connected=True,
                severity=payload.severity,
                source_time=_stamp_to_datetime(payload.stamp),
            )

        return self._submit(job, timeout=self._io_timeout + 5.0)

    def write(self, signal: str, value: float, command_id: UUID) -> Reading:
        del command_id  # CA 写入本身不带业务命令号；审计由上层执行服务记录
        if not self._connected:
            raise ConnectionError("EPICS 网关尚未连接")
        if signal not in self._paths:
            raise KeyError(f"未配置业务信号：{signal}")

        def job(ca) -> Reading:
            handle = self._channels[signal]
            if ca.ca_state(handle) != CS_CONN:
                ca.ca_pend_event(0.05)
                if ca.ca_state(handle) != CS_CONN:
                    raise ConnectionError(f"PV 未连接：{self._paths[signal]}")
            if not ca.ca_write_access(handle):
                raise PermissionError(f"PV 只读：{self._paths[signal]}")
            payload = ctypes.c_double(float(value))
            rc = ca.ca_array_put(DBR_DOUBLE, 1, handle, ctypes.byref(payload))
            ca.ca_pend_io(self._io_timeout)
            if rc != ECA_NORMAL:
                raw = ca.ca_message(rc)
                text = raw.decode("utf-8", "replace") if raw else f"CA 错误 {rc}"
                raise ConnectionError(f"写入失败（{self._paths[signal]}）：{text}")
            return self._reading(signal, float(value), connected=True, severity=0)

        return self._submit(job, timeout=self._io_timeout + 5.0)

    def snapshot(self, signals: list[str]) -> dict[str, Reading]:
        readings: dict[str, Reading] = {}
        for signal in signals:
            readings[signal] = self.read(signal)
        return readings

    def close(self) -> None:
        """停止专用线程并销毁 CA 上下文。"""
        if self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=5.0)
        self._connected = False

    # ---------- 内部工具 ----------

    def _reading(
        self,
        signal: str,
        value: float,
        connected: bool,
        severity: int | None,
        source_time: datetime | None = None,
    ) -> Reading:
        now = datetime.now(UTC)
        return Reading(
            signal=signal,
            value=value,
            unit=self._units.get(signal, ""),
            source_time=source_time or now,
            received_time=now,
            severity=severity if severity is not None else 0,
            connected=connected,
        )


def _stamp_to_datetime(stamp: _EpicsTimeStamp) -> datetime:
    """EPICS 时间戳（1990 纪元）换算为 UTC datetime；不可用时回退当前时间。"""
    if stamp.secPastEpoch == 0 and stamp.nsec == 0:
        return datetime.now(UTC)
    seconds = stamp.secPastEpoch + EPICS_EPOCH_OFFSET_S + stamp.nsec / 1e9
    return datetime.fromtimestamp(seconds, tz=UTC)
