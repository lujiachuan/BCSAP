"""扫谱任务的跨进程契约。

对应架构文档 6.4（状态机）与 6.5（扫描点流程）。关键约定：

* 横坐标记录的是**实际读回值**，不是下发过的设定值——设定值只是意图，
  磁场实际到哪由回读决定（文档 6.5：保存实际坐标）。
* 每个点带 ``quality``：写入被拒、回读超时、探测失败都要如实标注，
  不能把可疑点混进谱图当作好数据。
* ``ScanPoint.target`` 与 ``ScanPoint.coordinate`` 分开保存，
  供事后分析「设备实际跟随得怎么样」。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class ScanAxis(BaseModel):
    """扫描轴：一组同步移动的设定信号，加一路用于记录实际坐标的回读信号。

    成组扫描（如四路磁铁一起走）时 ``setpoint_signals`` 有多项，
    每点把这些信号写到同一个目标值，而 ``readback_signal`` 取其中一路
    的实际回读作为该点的坐标——成组时各路回读应当一致，不一致本身就是
    值得记录的现象（见 ``ScanPoint.coordinate_spread``）。
    """

    model_config = ConfigDict(strict=True)

    label: str
    setpoint_signals: list[str]
    readback_signal: str
    # 可选：把坐标换算成质量（u）的多项式系数 a0..a3，仅用于展示与导出
    mass_coefficients: list[float] = []


class ScanRunRequest(BaseModel):
    """一次扫谱任务的参数。"""

    model_config = ConfigDict(strict=True)

    axis: ScanAxis
    detector_signal: str
    start: float
    stop: float
    step: float
    # 每点驻留（积分）时间，单位秒
    dwell_s: float = 1.0
    # 每点重复采样次数，取中位数抗单次毛刺
    samples_per_point: int = 3
    # 回读稳定判据；不填则用映射里条目自己的 settle_tol/settle_timeout
    settle_tol: float | None = None
    settle_timeout_s: float = 20.0
    # 回读未稳定时的处理："record" 记下该点并标注质量；"fail" 直接判定任务失败
    on_unsettled: str = "record"

    @field_validator("start", "stop", "step", "dwell_s", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value

    def targets(self, limit: int = 20000) -> list[float]:
        """按 start/step 展开目标值序列（含两端，容差半个 step）。

        ``limit`` 是硬上限：点数算错时宁可拒绝，也不要让服务去跑一场
        几小时的扫描——那种错误在现场很难立刻发现。
        """
        span = self.stop - self.start
        if self.step == 0:
            raise ValueError("步长不能为 0")
        if span == 0:
            return [self.start]
        if (span > 0) != (self.step > 0):
            raise ValueError("步长方向与起止范围不一致")

        count = int(abs(span) / abs(self.step)) + 1
        if count > limit:
            raise ValueError(f"按当前参数将产生 {count} 个点，超过上限 {limit}")
        return [self.start + self.step * index for index in range(count)]


class ScanPoint(BaseModel):
    """一个扫描点的实测记录。"""

    model_config = ConfigDict(strict=True)

    index: int
    target: float
    coordinate: float
    signal: float
    quality: str
    at: str
    # 成组扫描时各路回读的最大差值；单路扫描恒为 0
    coordinate_spread: float = 0.0
    detail: str | None = None
    # 是否进入谱图（质量不合格的点不进 x/y 数组，但保留在点列表里可追溯）
    included: bool = True


class ScanRunStatus(BaseModel):
    """任务状态快照。``points`` 不随状态返回，单独走 points 端点。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    state: str
    label: str
    detector_signal: str
    total_points: int
    completed_points: int
    message: str
    started_at: str | None = None
    finished_at: str | None = None
    # 持久化结果（COMPLETED 后才有）
    spectrum_id: str | None = None
    spectrum_path: str | None = None
    sha256: str | None = None
    point_count: int | None = None
    # 被占用的设备组（诊断用）
    locked_groups: list[str] = []


class ScanPointsResponse(BaseModel):
    """已完成点的增量拉取结果。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    points: list[ScanPoint]
    total_points: int
