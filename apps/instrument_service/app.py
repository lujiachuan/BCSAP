"""仪器执行服务的 HTTP 应用。"""

from fastapi import FastAPI

from packages.contracts import ServiceStatus

from .pv_health import check_pv_health, create_simulated_gateway


def create_app() -> FastAPI:
    """创建仅供本机使用的执行服务应用。"""

    app = FastAPI(title="仪器执行服务", version="0.1.0")
    gateway = create_simulated_gateway()

    @app.get("/control/v1/status", response_model=ServiceStatus)
    def get_status() -> ServiceStatus:
        return ServiceStatus(
            service="instrument-service",
            status="ready",
            version=app.version,
            detail="模拟 EPICS 适配层可用；真实设备尚未接入",
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
        return check_pv_health(gateway)

    return app


app = create_app()
