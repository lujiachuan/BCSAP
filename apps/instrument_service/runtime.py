"""执行服务运行时：持有当前 PV 映射配置与网关，支持配置热更新。"""

from __future__ import annotations

from threading import RLock

from packages.contracts import PvHealthResponse, PvMappingConfig

from . import pv_mapping
from .pv_health import check_pv_health, create_gateway


class InstrumentRuntime:
    """把「当前配置 + 按配置构建的网关」放在一起管理。

    网关按需创建：只有真的要用（健康检查/读写）时才建，避免服务刚起来就
    去连 IOC。配置更新时关闭旧网关并换新，保证映射改动立即生效。
    """

    def __init__(self, config: PvMappingConfig | None = None) -> None:
        self._lock = RLock()
        self._config = config if config is not None else pv_mapping.load_config()
        self._gateway = None

    @property
    def config(self) -> PvMappingConfig:
        with self._lock:
            return self._config

    def gateway(self):
        with self._lock:
            if self._gateway is None:
                self._gateway = create_gateway(self._config)
            return self._gateway

    def apply(self, config: PvMappingConfig) -> None:
        """热更新配置：换上新网关并关闭旧网关。"""
        with self._lock:
            previous = self._gateway
            self._config = config
            self._gateway = create_gateway(config)
            if previous is not None:
                close = getattr(previous, "close", None)
                if callable(close):
                    close()

    def check_health(self) -> PvHealthResponse:
        with self._lock:
            return check_pv_health(self.gateway(), self._config)

    def close(self) -> None:
        with self._lock:
            if self._gateway is not None:
                close = getattr(self._gateway, "close", None)
                if callable(close):
                    close()
                self._gateway = None
