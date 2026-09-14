"""受控设备 PV 映射契约：业务信号 → EPICS PV。

除「信号 → PV」本身外，本契约还承载**执行层写入校验所需的安全参数**
（架构文档 8.1：设备配置应包含映射、范围、单位、稳定条件）：

* ``group``        设备分组，界面按组呈现，也是设备组锁的粒度；
* ``readback_signal`` 设定信号对应的回读信号，写后读回与稳定判据都用它；
* ``min_value`` / ``max_value`` 硬边界，执行层写前必查；
* ``max_step``     单次允许的最大变化量（超出则拒绝或按斜坡分步）；
* ``max_rate``     变化速率上限（单位/秒），斜坡分步的间隔由它决定；
* ``settle_tol`` / ``settle_timeout`` 回读稳定判据（容差与超时秒）。

这些字段全部可选：历史配置（只有前 6 个字段）照常读入，不会因缺字段而失败。
"""

from pydantic import BaseModel, ConfigDict, field_validator

# 数值类安全参数统一在 before 阶段把 int 归一成 float：
# 模型是 strict 的，而 JSON 里的 ``0`` 解析为 int，strict 模式下 float 字段会拒收。
_NUMERIC_FIELDS = (
    "min_value",
    "max_value",
    "max_step",
    "max_rate",
    "settle_tol",
    "settle_timeout",
)


class PvMappingEntry(BaseModel):
    """一条“业务信号 → PV”映射。

    signal 是执行服务内部引用的稳定键（如 ``gas.ar.flow_setpoint``），
    pv 是现场 IOC 上真实的 EPICS PV 名（如 ``Part1:Flow_W:CS200A:Setpoint``）。
    """

    model_config = ConfigDict(strict=True)

    signal: str
    label: str
    pv: str
    unit: str
    writable: bool
    required: bool

    # ---- 分组与回读配对 ----
    group: str = ""
    readback_signal: str = ""

    # ---- 信号角色：决定界面用什么控件呈现，也供扫描/调束区分设定与测量 ----
    #   "setpoint" 连续设定值（Spinbox + 写入 + 步进）
    #   "toggle"   0/1 开关（开/关按钮）
    #   "pulse"    一次性触发，写 1 后由设备自行复位（启动/停止/复位/灭弧）
    #   "readback" 只读测量值
    role: str = ""

    # ---- 执行层写入校验参数（None = 该项不校验）----
    min_value: float | None = None
    max_value: float | None = None
    max_step: float | None = None
    max_rate: float | None = None
    settle_tol: float | None = None
    settle_timeout: float | None = None

    @field_validator(*_NUMERIC_FIELDS, mode="before")
    @classmethod
    def _coerce_int_to_float(cls, value: object) -> object:
        """把 JSON 里的整数安全参数归一成 float，避免 strict 模式拒收。"""
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value

    @property
    def has_bounds(self) -> bool:
        """是否配置了可用的边界（至少一侧）。"""
        return self.min_value is not None or self.max_value is not None


# 允许的信号角色。空串表示未归类（历史配置），界面按 role 缺省处理。
SIGNAL_ROLES: tuple[str, ...] = ("setpoint", "toggle", "pulse", "readback")


class PvMappingConfig(BaseModel):
    """PV 映射，由执行服务持有并持久化。

    设备访问统一走真实 EPICS Channel Access，没有“模拟/真实”模式之分：
    没有 IOC 时健康检查就会如实报未连接。开发与联调请起 ``sim/`` 下的本地模拟 IOC。
    """

    model_config = ConfigDict(strict=True)

    version: int
    entries: list[PvMappingEntry]


class PvMappingIssue(BaseModel):
    """一条校验问题，index 为行号（-1 表示整体配置）。"""

    model_config = ConfigDict(strict=True)

    index: int
    field: str
    message: str


class PvMappingValidationError(BaseModel):
    """校验失败响应体。"""

    model_config = ConfigDict(strict=True)

    config_version: int
    issues: list[PvMappingIssue]
