"""仪器执行服务的 HTTP 应用。"""

from fastapi import FastAPI, HTTPException

from packages.contracts import (
    PvMappingConfig,
    PvMappingValidationError,
    ServiceStatus,
)

from . import pv_mapping
from .runtime import InstrumentRuntime


def create_app(runtime: InstrumentRuntime | None = None) -> FastAPI:
    """创建仅供本机使用的执行服务应用。

    ``runtime`` 可注入，测试时用临时配置建立独立运行时。
    """

    app = FastAPI(title="仪器执行服务", version="0.1.0")
    state = runtime if runtime is not None else InstrumentRuntime()

    @app.get("/control/v1/status", response_model=ServiceStatus)
    def get_status() -> ServiceStatus:
        config = state.config
        mode = (
            "真实 EPICS 通道访问"
            if config.gateway == "channel-access"
            else "模拟 EPICS 适配层"
        )
        return ServiceStatus(
            service="instrument-service",
            status="ready",
            version=app.version,
            detail=f"网关：{mode}；PV 映射 {len(config.entries)} 条",
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
