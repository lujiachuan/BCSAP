"""共享数据服务的 HTTP 应用。"""

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from packages.contracts import ServiceStatus, SyncChanges, SyncManifest

from .pg import ping
from .showcase_api import router as showcase_router
from .sync_catalog import (
    SPECTRUM_CONTENT,
    SYNC_CURSOR,
    sync_records,
    sync_spectra,
)

_SHOWCASE_UI = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Showcase · cluster_ms</title>
<style>
body{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:980px;color:#1a1a1a}
h1{font-size:20px;margin:0 0 6px} .muted{color:#666;margin:0 0 16px}
input,button{font:inherit;padding:6px 10px} button{cursor:pointer}
table{border-collapse:collapse;width:100%;margin-top:12px}
th,td{border:1px solid #ddd;padding:6px 8px;text-align:left} th{background:#f5f5f5}
tr:hover{background:#f0f7ff;cursor:pointer}
#meta{white-space:pre-wrap;background:#f7f7f7;padding:12px;border-radius:6px;margin-top:14px}
</style></head><body>
<h1>Showcase 只读预览</h1>
<p class="muted">data_service → PostgreSQL <code>cluster_ms</code></p>
<p id="status">…</p>
<p><input id="q" value="Ag"/><button id="btn">查询</button>
<a href="/docs" style="margin-left:12px">OpenAPI /docs</a></p>
<table><thead><tr><th>ID</th><th>名称</th><th>金属</th><th>类</th><th>标注</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="meta"></div>
<script>
const B='/api/v1/showcase';
async function status(){const j=await (await fetch(B+'/status')).json();
 document.getElementById('status').textContent=`${j.status} · ${j.detail||''}`;}
async function list(){const q=document.getElementById('q').value.trim();
 const j=await (await fetch(B+'/spectra?limit=30'+(q?'&q='+encodeURIComponent(q):''))).json();
 const tb=document.getElementById('rows'); tb.innerHTML='';
 for(const i of j.items||[]){const tr=document.createElement('tr');
  tr.innerHTML=`<td>${i.spectrum_id}</td><td>${i.name}</td><td>${i.metal||''}</td><td>${i.isotope_class}</td><td>${i.n_annotations}</td>`;
  tr.onclick=()=>meta(i.spectrum_id); tb.appendChild(tr);} }
async function meta(id){const box=document.getElementById('meta'); box.textContent='加载 #'+id;
 const j=await (await fetch(B+'/spectra/'+id)).json();
 const peaks=(j.peaks||[]).slice(0,20).map(p=>`${p.peak_mass}\\t${p.formula||''}\\t${p.err??''}`).join('\\n');
 box.textContent=`#${j.spectrum_id} ${j.name}\\nmetal=${j.metal} class=${j.isotope_class}\\npolicy=${j.axis_policy}\\nmethod=${j.calibration_method||''}\\n\\npeaks(20/${(j.peaks||[]).length}):\\n${peaks}`;}
document.getElementById('btn').onclick=list; status().then(list);
</script></body></html>"""


def create_app() -> FastAPI:
    """创建共享数据服务应用。"""

    app = FastAPI(title="谱图平台数据服务", version="0.1.0")
    app.include_router(showcase_router)

    @app.get("/showcase", response_class=HTMLResponse)
    def showcase_ui() -> str:
        return _SHOWCASE_UI

    @app.get("/api/v1/health/live", response_model=ServiceStatus)
    def get_liveness() -> ServiceStatus:
        return ServiceStatus(
            service="data-service",
            status="ready",
            version=app.version,
        )

    @app.get("/api/v1/health/ready", response_model=ServiceStatus)
    def get_readiness() -> ServiceStatus:
        info = ping()
        if info.get("ok"):
            return ServiceStatus(
                service="data-service",
                status="ready",
                version=app.version,
                detail=f"Showcase 已接 {info.get('dsn_hint')}，spectra={info.get('n_spectra')}",
            )
        return ServiceStatus(
            service="data-service",
            status="degraded",
            version=app.version,
            detail=f"服务框架可用；PostgreSQL 不可达: {info.get('error')}",
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
