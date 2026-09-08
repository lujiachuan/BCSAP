"""共享数据服务的 HTTP 应用。"""

from fastapi import FastAPI, HTTPException, Query, Response

from packages.contracts import ServiceStatus, SyncChanges, SyncManifest

from .sync_catalog import (
    SPECTRUM_CONTENT,
    SYNC_CURSOR,
    sync_records,
    sync_spectra,
)


def create_app() -> FastAPI:
    """创建共享数据服务应用。"""

    app = FastAPI(title="谱图平台数据服务", version="0.1.0")

    @app.get("/api/v1/health/live", response_model=ServiceStatus)
    def get_liveness() -> ServiceStatus:
        return ServiceStatus(
            service="data-service",
            status="ready",
            version=app.version,
        )

    @app.get("/api/v1/health/ready", response_model=ServiceStatus)
    def get_readiness() -> ServiceStatus:
        return ServiceStatus(
            service="data-service",
            status="degraded",
            version=app.version,
            detail="服务框架可用；PostgreSQL 尚未配置",
        )

    @app.get("/api/v1/sync/manifest", response_model=SyncManifest)
    def get_sync_manifest() -> SyncManifest:
        spectra = sync_spectra()
        return SyncManifest(
            cursor=SYNC_CURSOR,
            record_count=len(sync_records()),
            spectrum_count=len(spectra),
            total_bytes=sum(item["byte_length"] for item in spectra),
        )

    @app.get("/api/v1/sync/changes", response_model=SyncChanges)
    def get_sync_changes(cursor: str | None = Query(default=None)) -> SyncChanges:
        if cursor == SYNC_CURSOR:
            return SyncChanges(
                next_cursor=SYNC_CURSOR,
                has_more=False,
                records=[],
                spectra=sync_spectra(),
                deleted=[],
            )
        return SyncChanges(
            next_cursor=SYNC_CURSOR,
            has_more=False,
            records=sync_records(),
            spectra=sync_spectra(),
            deleted=[],
        )

    @app.get("/api/v1/spectra/{spectrum_id}/content")
    def download_spectrum(spectrum_id: str) -> Response:
        payload = SPECTRUM_CONTENT.get(spectrum_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="谱图不存在")
        checksum = next(
            item["sha256"] for item in sync_spectra() if item["id"] == spectrum_id
        )
        return Response(
            content=payload,
            media_type="application/x-npz",
            headers={"ETag": f'"{checksum}"', "X-Content-SHA256": checksum},
        )

    return app


app = create_app()
