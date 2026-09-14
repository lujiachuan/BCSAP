"""受控信号读写的跨进程契约。

架构前提（架构文档 6.2）：**执行服务是硬件操作的唯一应用入口**，客户端不直连 IOC。
因此读值与写值都必须经过本契约定义的请求/响应，而不是界面里直接调 pyepics。

写入请求/响应刻意把「请求值」与「实际生效值」「回读值」分开记录
（架构文档 9.4：保存建议值与实际读回值，不能把建议值当成已执行值）。
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class SignalReading(BaseModel):
    """单个业务信号的一次读数。"""

    model_config = ConfigDict(strict=True)

    signal: str
    pv: str
    value: float | None
    unit: str
    connected: bool
    writable: bool
    severity: int | None = None
    source_time: str | None = None
    received_time: str | None = None
    detail: str | None = None


class SignalSnapshotRequest(BaseModel):
    """批量读取请求；``signals`` 为空表示读当前映射里的全部信号。"""

    model_config = ConfigDict(strict=True)

    signals: list[str] = []


class SignalSnapshot(BaseModel):
    """批量读取结果。"""

    model_config = ConfigDict(strict=True)

    taken_at: str
    readings: list[SignalReading]


class SignalWriteRequest(BaseModel):
    """单点写入请求。

    ramp=True 时，若变化量超过条目的 ``max_step``，执行层按斜坡分步写入
    （步间隔由 ``max_rate`` 决定）；ramp=False 时超单步直接拒绝。
    dry_run=True 只做校验、不写设备，供界面预检。
    """

    model_config = ConfigDict(strict=True)

    signal: str
    value: float
    command_id: str | None = None
    ramp: bool = True
    dry_run: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value_to_float(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value

    @field_validator("command_id")
    @classmethod
    def _check_command_id(cls, value: str | None) -> str | None:
        """command_id 用于写入幂等，必须是可解析的 UUID，避免调用方随手填字符串。"""
        if value is None:
            return None
        try:
            UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("command_id 必须是 UUID 字符串") from exc
        return value


class SignalWriteResult(BaseModel):
    """写入结果：是否受理、实际执行的斜坡步、以及写后回读。

    被拒绝时 ``accepted=False`` 且 ``reason`` 说明原因，``applied`` 为 None——
    设备未被改动。
    """

    model_config = ConfigDict(strict=True)

    signal: str
    pv: str
    unit: str
    accepted: bool
    requested: float
    applied: float | None = None
    readback: float | None = None
    previous: float | None = None
    ramp_steps: list[float] = []
    reason: str | None = None
    command_id: str | None = None
    dry_run: bool = False
    # 写入失败时，设备实际状态是否未知。文档 9.3：下发命令后的响应丢失会形成
    # 「是否已执行未知」，此类失败不能当成「设备没动」，也不能自动重试。
    device_state_unknown: bool = False
    finished_at: str


class SignalBatchWriteRequest(BaseModel):
    """成组写入请求（一次下发一组信号，如磁铁 1+2 / 3+4 / 1~4）。

    **为什么必须由执行服务成批做**：界面循环调单点写接口时，第三台失败就会留下
    "前两台已经动了、后两台还在原位"的部分成功状态——磁场不均匀比整体不动更危险，
    而且没有地方记录"这一批本来是一起下的"（改造报告 §4.2）。

    ``atomic=True``：先对**全部**成员做一次干跑校验（边界/单步/速率/可写），
    任何一项不过就整批不下发，一次都不写设备。
    """

    model_config = ConfigDict(strict=True)

    writes: list[SignalWriteRequest]
    atomic: bool = True
    # 成组动作的说明，写进结果消息与日志，便于现场复盘"这次一起下的是什么"
    note: str = ""


class SignalBatchWriteResponse(BaseModel):
    """成组写入结果：逐路明细 + 明确的整体结论。

    三种结局要分得清：
    * ``ok=True`` —— 全部受理并写入；
    * ``ok=False`` 且 ``nothing_written=True`` —— **整批都没写**（原子校验没过，
      或设备组被占）；
    * ``ok=False`` 但有部分 ``accepted`` —— 执行途中失败（设备异常），
      ``results`` 里逐路标明谁成功、谁状态未知。
    """

    model_config = ConfigDict(strict=True)

    ok: bool
    message: str
    requested: int
    accepted: int
    rejected: int
    results: list[SignalWriteResult] = []
    locked_groups: list[str] = []
    # 整批都没写时为 True（原子校验阶段或抢锁阶段就挡住了），界面据此措辞
    nothing_written: bool = False
