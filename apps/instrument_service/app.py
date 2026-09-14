"""仪器执行服务的 HTTP 应用。"""

from fastapi import FastAPI, HTTPException

from packages.contracts import (
    PvMappingConfig,
    PvMappingValidationError,
    ServiceStatus,
    SignalSnapshot,
    SignalSnapshotRequest,
    SignalWriteRequest,
    SignalWriteResult,
)
from packages.contracts.scan import (
    ScanPointsResponse,
    ScanRunRequest,
    ScanRunStatus,
)
from packages.contracts.tuning import (
    TuningIterationsResponse,
    TuningRunRequest,
    TuningRunStatus,
)

from . import pv_mapping
from .runtime import InstrumentRuntime
from .scan_service import ScanError
from .scan_store import StoreUnavailable
from .tuning_service import TuningError


def create_app(runtime: InstrumentRuntime | None = None) -> FastAPI:
    """创建仅供本机使用的执行服务应用。

    ``runtime`` 可注入，测试时用临时配置建立独立运行时。
    """

    app = FastAPI(title="仪器执行服务", version="0.1.0")
    state = runtime if runtime is not None else InstrumentRuntime()

    @app.get("/control/v1/status", response_model=ServiceStatus)
    def get_status() -> ServiceStatus:
        config = state.config
        return ServiceStatus(
            service="instrument-service",
            status="ready",
            version=app.version,
            detail=f"真实 EPICS 通道访问；PV 映射 {len(config.entries)} 条",
        )

    @app.get("/control/v1/health/live", response_model=ServiceStatus)
    def get_liveness() -> ServiceStatus:
        return ServiceStatus(
            service="instrument-service",
            status="ready",
            version=app.version,
        )

    @app.get("/control/v1/pvs/health")
    def get_pv_health():
        return state.check_health()

    @app.post("/control/v1/signals/read", response_model=SignalSnapshot)
    def post_signals_read(request: SignalSnapshotRequest) -> SignalSnapshot:
        """批量读取受控信号；``signals`` 为空表示读当前映射的全部信号。

        单个信号读失败只把该项标为未连接，不影响其余读数——界面可以照常
        显示其余实时值。
        """
        return state.signals.read_snapshot(request.signals)

    @app.post("/control/v1/signals/write", response_model=SignalWriteResult)
    def post_signals_write(request: SignalWriteRequest) -> SignalWriteResult:
        """写入单个受控信号，由执行层完成边界/单步/速率校验。

        **无论受理与否都返回 200**：被拒绝时 ``accepted=False`` 且 ``reason``
        说明原因（设备未被改动），供界面原样展示给操作员；这样调用方只需处理
        一种响应形状，不必在异常与正常返回之间分叉。

        带 ``command_id`` 的重复请求返回首次结果，不会重复写设备。
        """
        return state.signals.write(request)

    # ------------------------------------------------------------------
    # 扫谱任务（架构文档 6.4 / 6.5）
    # ------------------------------------------------------------------
    @app.post("/control/v1/scan/runs", response_model=ScanRunStatus)
    def post_scan_run(request: ScanRunRequest) -> ScanRunStatus:
        """创建并启动一次扫谱。

        参数或映射有问题返回 400；同时只允许一个扫谱任务，重复启动返回 409。
        启动即返回初始状态，进度由 GET 状态/点端点轮询。
        """
        try:
            return state.scan.start(request)
        except StoreUnavailable as exc:
            # 暂存目录不可写 → 采集了也存不下来，按服务不可用处理而不是请求错误
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ScanError as exc:
            detail = str(exc)
            # 「已有任务在进行中」是并发冲突，不是请求写错了
            code = 409 if "在进行中" in detail else 400
            raise HTTPException(status_code=code, detail=detail) from exc

    @app.get("/control/v1/scan/runs/{run_id}", response_model=ScanRunStatus)
    def get_scan_run(run_id: str) -> ScanRunStatus:
        try:
            return state.scan.status(run_id)
        except ScanError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/control/v1/scan/runs/{run_id}/points", response_model=ScanPointsResponse)
    def get_scan_points(run_id: str, since: int = 0) -> ScanPointsResponse:
        """增量拉取已完成点：``since`` 传上次拿到的最大 index + 1。"""
        try:
            return state.scan.points(run_id, since)
        except ScanError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/control/v1/scan/runs/{run_id}/stop", response_model=ScanRunStatus)
    def post_scan_stop(run_id: str) -> ScanRunStatus:
        """请求停止；收尾由执行线程完成，需继续轮询状态直到进入终态。"""
        try:
            return state.scan.stop(run_id)
        except ScanError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/control/v1/scan/runs/{run_id}/acknowledge", response_model=ScanRunStatus
    )
    def post_scan_acknowledge(run_id: str, note: str = "") -> ScanRunStatus:
        """人工确认设备状态后释放恢复锁。

        ``RECOVERY_REQUIRED`` 的任务会一直占着设备组不放，防止别的任务在
        「设备实际状态未知」的前提下继续驱动同一批设备。操作员现场核对完
        设备状态后调用本端点解锁（文档 6.3：恢复动作由明确策略决定）。
        """
        try:
            return state.scan.acknowledge_recovery(run_id, note)
        except ScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 调束任务（架构文档 6.6）
    # ------------------------------------------------------------------
    @app.post("/control/v1/tuning/runs", response_model=TuningRunStatus)
    def post_tuning_run(request: TuningRunRequest) -> TuningRunStatus:
        """创建并启动一次调束。

        第一版只开放「建议 → 人工确认」（``mode="confirm"``）：启动后返回
        ``awaiting_confirmation`` 与候选参数，必须调 ``/approve`` 才会写设备。
        其它模式返回 400，连续自动写入需另行通过安全评审。
        """
        try:
            return state.tuning.start(request)
        except TuningError as exc:
            detail = str(exc)
            code = 409 if "在进行中" in detail else 400
            raise HTTPException(status_code=code, detail=detail) from exc

    @app.get("/control/v1/tuning/runs/{run_id}", response_model=TuningRunStatus)
    def get_tuning_run(run_id: str) -> TuningRunStatus:
        try:
            return state.tuning.status(run_id)
        except TuningError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        "/control/v1/tuning/runs/{run_id}/iterations",
        response_model=TuningIterationsResponse,
    )
    def get_tuning_iterations(run_id: str) -> TuningIterationsResponse:
        try:
            return state.tuning.iterations(run_id)
        except TuningError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/control/v1/tuning/runs/{run_id}/approve", response_model=TuningRunStatus
    )
    def post_tuning_approve(run_id: str) -> TuningRunStatus:
        """人工确认当前候选：执行层校验后写设备、读回、测量目标并记录本轮。

        同步返回：写入与稳定等待都在这一个请求里完成，操作员本来就在等结果。
        """
        try:
            return state.tuning.approve(run_id)
        except TuningError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/control/v1/tuning/runs/{run_id}/stop", response_model=TuningRunStatus)
    def post_tuning_stop(run_id: str) -> TuningRunStatus:
        try:
            return state.tuning.stop(run_id)
        except TuningError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/control/v1/tuning/runs/{run_id}/acknowledge", response_model=TuningRunStatus
    )
    def post_tuning_acknowledge(run_id: str, note: str = "") -> TuningRunStatus:
        """人工确认设备状态后释放恢复锁（与扫谱同义）。"""
        try:
            return state.tuning.acknowledge_recovery(run_id, note)
        except TuningError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/control/v1/pv-mapping", response_model=PvMappingConfig)
    def get_pv_mapping() -> PvMappingConfig:
        return state.config

    @app.put("/control/v1/pv-mapping", response_model=PvMappingConfig)
    def put_pv_mapping(config: PvMappingConfig) -> PvMappingConfig:
        """校验并保存 PV 映射，成功后立即对后续读写生效。

        校验失败返回 400 + 逐行问题清单，客户端按行标红。
        """
        issues = pv_mapping.validate_config(config)
        if issues:
            payload = PvMappingValidationError(
                config_version=pv_mapping.CONFIG_VERSION, issues=issues
            )
            raise HTTPException(status_code=400, detail=payload.model_dump())
        try:
            pv_mapping.save_config(config)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"PV 映射写入失败：{exc}"
            ) from exc
        state.apply(config)
        return state.config

    return app


app = create_app()
