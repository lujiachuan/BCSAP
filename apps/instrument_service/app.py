"""仪器执行服务的 HTTP 应用。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from packages.contracts import (
    ManualDashboardConfig,
    PvMappingConfig,
    PvMappingValidationError,
    ServiceStatus,
    SignalBatchWriteRequest,
    SignalBatchWriteResponse,
    SignalSnapshot,
    SignalSnapshotRequest,
    SignalWriteRequest,
    SignalWriteResult,
)
from packages.contracts.scan import (
    MagnetRetractRequest,
    MagnetRetractResponse,
    ScanPointsResponse,
    ScanRunRequest,
    ScanRunStatus,
)
from packages.contracts.tuning import (
    TuningCatalog,
    TuningFinalizeRequest,
    TuningFinalizeResult,
    TuningIterationsResponse,
    TuningRunRequest,
    TuningRunStatus,
)

from . import manual_dashboard, pv_mapping, tuning_analysis_service, tuning_catalog
from .device_locks import DeviceBusy
from .pv_health import create_gateway, create_simulated_gateway
from .runtime import InstrumentRuntime
from .scan_service import ScanError
from .scan_store import StoreUnavailable
from .signal_io import WriteRejected
from .tuning_service import TuningError


def _gateway_factory_from_env():
    """SPECTRUM_USE_SIM=1 时走内存 SIM 网关（无硬件联调用）。"""
    import os
    if os.environ.get("SPECTRUM_USE_SIM", "").strip().lower() in {"1", "true", "yes"}:
        return create_simulated_gateway
    return create_gateway


def _start_optuna_dashboard(runtime) -> None:
    """后台起 optuna-dashboard：浏览器看 Optuna 原生交互图（history/importance/slice/contour）。

    端口 8001；绑失败或没装 optuna-dashboard 时静默跳过，不影响主服务。
    """
    import threading
    try:
        import optuna_dashboard  # noqa: F401
    except ImportError:
        return
    storage_url = runtime.tuning.optuna_storage_url
    port = 8001

    def _serve() -> None:
        try:
            import optuna
            storage = optuna.storages.RDBStorage(url=storage_url)
            optuna_dashboard.run_server(storage, host="127.0.0.1", port=port)
        except Exception as exc:  # noqa: BLE001
            print(f"[optuna-dashboard] 未启动：{exc}")

    t = threading.Thread(target=_serve, daemon=True, name="optuna-dashboard")
    t.start()
    runtime.optuna_dashboard_started = True
    print(f"[optuna-dashboard] http://127.0.0.1:{port}")


def create_app(runtime: InstrumentRuntime | None = None) -> FastAPI:
    """创建仅供本机使用的执行服务应用。

    ``runtime`` 可注入，测试时用临时配置建立独立运行时。
    """

    state = runtime if runtime is not None else InstrumentRuntime(gateway_factory=_gateway_factory_from_env())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.recover_startup()
        _start_optuna_dashboard(state)
        yield

    app = FastAPI(title="仪器执行服务", version="0.1.0", lifespan=lifespan)

    @app.get("/control/v1/status", response_model=ServiceStatus)
    def get_status() -> ServiceStatus:
        config = state.config
        read_only = state.read_only
        detail = f"EPICS caget/caput 命令行访问；PV 映射 {len(config.entries)} 条"
        status = "ready"
        if state.config_error:
            status = "degraded"
            detail += f"；配置不可用，已进入只读保护：{state.config_error}"
        if state.deployment_read_only:
            detail += "；**全局只读模式**（部署参数启用，所有写入被拒绝）"
        if state.startup_recoveries:
            status = "degraded"
            detail += f"；有 {len(state.startup_recoveries)} 个重启恢复项待人工确认"
        return ServiceStatus(
            service="instrument-service",
            status=status,
            version=app.version,
            detail=detail,
            read_only=read_only,
            read_only_reason=(
                "deployment"
                if state.deployment_read_only
                else ("configuration" if state.config_error else None)
            ),
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

    @app.get("/control/v1/recovery")
    def get_recovery_items():
        return {"items": state.startup_recoveries}

    @app.post("/control/v1/recovery/{run_id}/acknowledge")
    def acknowledge_recovery_item(run_id: str, note: str = ""):
        try:
            return state.acknowledge_startup_recovery(run_id, note)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/control/v1/recovery/acknowledge-all")
    def acknowledge_all_recovery_items(payload: dict):
        note = str(payload.get("note") or "")
        return {"items": state.acknowledge_all_startup_recoveries(note)}

    @app.post("/control/v1/signals/read", response_model=SignalSnapshot)
    def post_signals_read(request: SignalSnapshotRequest) -> SignalSnapshot:
        """批量读取受控信号；``signals`` 为空表示读当前映射的全部信号。

        单个信号读失败只把该项标为未连接，不影响其余读数——界面可以照常
        显示其余实时值。

        **但请求了映射里不存在的信号名要明确报 400**：以前这里未捕获异常，
        调用方只看到"HTTP 500"，完全不知道是哪个信号名写错了（导出/快照这类
        一次读几十路的场景很容易踩到）。
        """
        try:
            return state.signals.read_snapshot(request.signals)
        except WriteRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/control/v1/signals/write", response_model=SignalWriteResult)
    def post_signals_write(request: SignalWriteRequest) -> SignalWriteResult:
        """写入单个受控信号，由执行层完成边界/单步/速率校验。

        **无论受理与否都返回 200**：被拒绝时 ``accepted=False`` 且 ``reason``
        说明原因（设备未被改动），供界面原样展示给操作员；这样调用方只需处理
        一种响应形状，不必在异常与正常返回之间分叉。

        带 ``command_id`` 的重复请求返回首次结果，不会重复写设备。
        """
        return state.write_signal(request)

    @app.post("/control/v1/signals/write-batch", response_model=SignalBatchWriteResponse)
    def post_signals_write_batch(
        request: SignalBatchWriteRequest,
    ) -> SignalBatchWriteResponse:
        """成组写入（磁铁 1+2 / 3+4 / 1~4 这类一起下发的动作）。

        ``atomic=true`` 时先整批干跑校验，任何一项不合格就整批不下发——
        界面循环调单点接口会留下"前两台动了、后两台没动"的中间状态。
        执行途中的失败逐路返回，不合并成一句"失败"。
        """
        return state.write_batch(
            request.writes, atomic=request.atomic, note=request.note
        )

    # ------------------------------------------------------------------
    # 调束可选项（目标白名单 / 变量白名单 / 束线上游关系）
    # ------------------------------------------------------------------
    @app.get("/control/v1/tuning/catalog", response_model=TuningCatalog)
    def get_tuning_catalog() -> TuningCatalog:
        """按**当前**映射与束线拓扑给出可选目标与变量。"""
        return tuning_catalog.build_catalog(state.config)

    @app.get("/control/v1/tuning/capabilities")
    def get_tuning_capabilities() -> dict:
        """引擎能力探测：界面按此显示可用引擎，不维护静态清单。"""
        import importlib.util
        cmaes_available = importlib.util.find_spec("cmaes") is not None
        return {
            "default_engine": "cmaes",
            "engines": [
                {"key": "tpe", "available": True, "reason": ""},
                {"key": "gp", "available": True, "reason": "兼容旧配置"},
                {
                    "key": "cmaes",
                    "available": cmaes_available,
                    "reason": "" if cmaes_available else "缺少 cmaes 依赖",
                },
                {"key": "random", "available": True, "reason": ""},
                {"key": "qmc", "available": True, "reason": ""},
            ],
            "dashboard": {
                "available": getattr(state, "optuna_dashboard_started", False),
                "url": "http://127.0.0.1:8001/",
            },
        }

    @app.get("/control/v1/tuning/runs")
    def get_tuning_runs(limit: int = 50) -> dict:
        """历史调束 run 列表（服务重启后仍可查）。"""
        rows = state.tuning._store.list_runs(limit=limit)
        return {
            "runs": [
                {
                    "run_id": r["run_id"],
                    "started_at": r["created_at"],
                    "finished_at": r["finished_at"],
                    "state": r["state"],
                    "message": r["message"],
                    "algorithm": r["algorithm"],
                    "best_objective": r["best_objective"],
                    "max_iterations": r["max_iterations"],
                    "mode": r["mode"],
                                    }
                for r in rows
            ]
        }

    # ------------------------------------------------------------------
    # 成组回落（与扫谱收尾同一套服务端逻辑）
    # ------------------------------------------------------------------
    @app.post("/control/v1/magnets/retract", response_model=MagnetRetractResponse)
    def post_magnet_retract(request: MagnetRetractRequest) -> MagnetRetractResponse:
        """把一组磁铁退到安全值：逐路写速率、逐路写电流、逐路等回读到到位。

        设备组被别人（扫谱/调束）占用时返回 409，不做"绕过锁偷偷写"的处理：
        回落本身是安全动作，但和别人的任务抢同一台设备只会制造未知状态。

        全局只读部署下不单开 400：每一路写都被执行层拒掉，逐路的理由原样回到
        ``ok=false`` 的 ``message`` 里（与单点/成组写入「200 + reason」同一口径）。
        """
        try:
            outcome = state.retract_magnets(
                request.setpoint_signals,
                request.current_a,
                request.rate_a_s,
                timeout_s=request.timeout_s,
            )
        except DeviceBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WriteRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MagnetRetractResponse(
            ok=outcome.ok, message=outcome.message, applied=outcome.applied
        )

    # ------------------------------------------------------------------
    # 扫谱任务（架构文档 6.4 / 6.5）
    # ------------------------------------------------------------------
    @app.post("/control/v1/scan/runs", response_model=ScanRunStatus)
    def post_scan_run(request: ScanRunRequest) -> ScanRunStatus:
        """创建并启动一次扫谱。

        参数或映射有问题返回 400；同时只允许一个扫谱任务，重复启动返回 409。
        启动即返回初始状态，进度由 GET 状态/点端点轮询。
        """
        if state.read_only:
            # 与其让每一步都在写入处被拒（"跑到一半才知道"，还可能留下半截状态），
            # 不如在启动阶段就说清：只读部署下扫谱这件事本身不成立
            raise HTTPException(
                status_code=400,
                detail=(
                    "全局只读模式：本执行服务按部署参数禁用了所有写入，扫谱无法执行。"
                    "需要扫谱请去掉只读参数并重启服务。"
                ),
            )
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
        if state.read_only:
            raise HTTPException(
                status_code=400,
                detail=(
                    "全局只读模式：本执行服务按部署参数禁用了所有写入，调束无法执行。"
                    "需要调束请去掉只读参数并重启服务。"
                ),
            )
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
    def get_tuning_iterations(
        run_id: str, after_iteration: int = -1
    ) -> TuningIterationsResponse:
        try:
            resp = state.tuning.iterations(run_id)
            if after_iteration >= 0:
                resp.iterations = [
                    it for it in resp.iterations
                    if getattr(it, "iteration", 0) > after_iteration
                ]
            return resp
        except TuningError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/control/v1/tuning/runs/{run_id}/analysis")
    def get_tuning_analysis(run_id: str):
        """结束后分析：爬山图数据 / 参数重要性 / 一维切片 / trial 表。

        数据从持久化 Optuna study 取，前端用 pyqtgraph 画图。
        """
        storage_url = state.tuning.optuna_storage_url
        result = tuning_analysis_service.analyze(storage_url, run_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="无可用 study 数据（未用 Optuna 引擎或未跑过）",
            )
        return result

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

    @app.post(
        "/control/v1/tuning/runs/{run_id}/pause", response_model=TuningRunStatus
    )
    def post_tuning_pause(run_id: str) -> TuningRunStatus:
        """暂停任务：不在写设备时可暂停，参数保持现状，resume 后继续。"""
        try:
            return state.tuning.pause(run_id)
        except TuningError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/control/v1/tuning/runs/{run_id}/resume", response_model=TuningRunStatus
    )
    def post_tuning_resume(run_id: str) -> TuningRunStatus:
        """继续暂停的任务：有挂起候选则按模式继续执行 / 等待确认。"""
        try:
            return state.tuning.resume(run_id)
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

    @app.post(
        "/control/v1/tuning/runs/{run_id}/finalize",
        response_model=TuningFinalizeResult,
    )
    def post_tuning_finalize(
        run_id: str, request: TuningFinalizeRequest
    ) -> TuningFinalizeResult:
        """结束后的设备处置：应用最优参数 / 恢复启动前参数 / 回安全值。

        三种动作都写设备，必须 ``confirm=true``；任务没结束、或处于恢复待确认状态
        （设备实际状态未知）时一律拒绝。设备组被别的任务占用时返回 409。
        """
        try:
            return state.tuning.finalize(run_id, request.action, confirm=request.confirm)
        except TuningError as exc:
            detail = str(exc)
            code = 409 if "设备组当前不可用" in detail else 400
            raise HTTPException(status_code=code, detail=detail) from exc

    @app.get("/control/v1/pv-mapping", response_model=PvMappingConfig)
    def get_pv_mapping() -> PvMappingConfig:
        return state.config

    @app.get("/control/v1/manual-dashboard", response_model=ManualDashboardConfig)
    def get_manual_dashboard() -> ManualDashboardConfig:
        return manual_dashboard.load_config(state.config)

    @app.put("/control/v1/manual-dashboard", response_model=ManualDashboardConfig)
    def put_manual_dashboard(config: ManualDashboardConfig) -> ManualDashboardConfig:
        """保存纯展示配置；不重建网关，也不改变设备锁和安全边界。"""
        try:
            manual_dashboard.save_config(config, state.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return config

    @app.put("/control/v1/pv-mapping", response_model=PvMappingConfig)
    def put_pv_mapping(
        config: PvMappingConfig, confirm_shrink: bool = False
    ) -> PvMappingConfig:
        """校验并保存 PV 映射，成功后立即对后续读写生效。

        校验失败返回 400 + 逐行问题清单，客户端按行标红。

        **条目数骤减要显式确认**（``?confirm_shrink=true``）：映射决定"谁能写、
        写到多少"，把 128 条存成 3 条会让没列出的设备全部失去映射（实测踩过一次：
        一个测试忘了把请求换成桩，直接把现场映射覆盖成 3 条测试数据）。
        """
        if state.deployment_read_only:
            # 映射决定"谁能写、写到多少"——只读部署下改它等于绕过只读本身，
            # 所以这里按写操作拒绝，而不是当成普通配置读写放行
            raise HTTPException(
                status_code=400,
                detail=(
                    "全局只读模式：本执行服务按部署参数禁用了所有写入，PV 映射不可修改。"
                    "需要改映射请去掉只读参数并重启服务。"
                ),
            )
        blocker = state.mapping_update_blocker()
        if blocker:
            raise HTTPException(status_code=409, detail=blocker)
        issues = pv_mapping.validate_config(config)
        if issues:
            payload = PvMappingValidationError(
                config_version=pv_mapping.CONFIG_VERSION, issues=issues
            )
            raise HTTPException(status_code=400, detail=payload.model_dump())
        shrink = pv_mapping.shrink_warning(len(state.config.entries), len(config.entries))
        if shrink and not confirm_shrink:
            raise HTTPException(status_code=400, detail=shrink)
        try:
            owner = state.acquire_mapping_update(config)
        except DeviceBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            try:
                pv_mapping.save_config(config)
            except OSError as exc:
                raise HTTPException(
                    status_code=500, detail=f"PV 映射写入失败：{exc}"
                ) from exc
            state.apply(config)
        finally:
            state.release_mapping_update(owner)
        return state.config

    return app


app = create_app()
