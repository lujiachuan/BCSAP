"""按侧边栏入口组织的页面包：每个页面一个模块，聚合后供主窗口装配。

拆分约定（多人并行开发页面时遵守，冲突只会在共享层出现）：

- 新增或修改某个页面时，只编辑 ``pages/`` 下该页面自己的模块；
  页面内容、标题、图标、所属分区都在本模块的 ``PAGE_SPEC`` 中自述。
- 页面之间不相互 import 具体页面类；跨页跳转通过主窗口信号完成。
- 共享控件与设计令牌仍来自 ``apps.desktop_client.widgets``、
  ``theme``、``surfaces`` 等公共模块，改动这些文件视为公共层变更。
- 想调整侧栏展示顺序：改下面聚合列表（先“实验控制”组，后“数据与系统”组），
  不要改各页面模块内部的注册内容。

主窗口通过 ``SECTIONS`` + ``page_specs()`` 装配侧栏与堆叠页面，不感知具体页面。
"""

from __future__ import annotations

from apps.desktop_client.pages import (  # noqa: F401
    analysis,
    library,
    samples,
    scan,
    settings,
    sync,
    tuning,
    workbench,
)

# 兼容性再导出：历史路径 apps.desktop_client.pages.X 继续可用。
from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import (  # noqa: F401
    SECTIONS,
    PageSpec,
    page_specs,
    register,
)
from apps.desktop_client.pages.scan import ScanPage
from apps.desktop_client.pages.settings import (
    DEFAULT_SERVICE_URLS,
    SystemSettingsPage,
    _valid_service_url,
)
from apps.desktop_client.pages.tuning import TuningPage
from apps.desktop_client.pages.workbench import WorkbenchPage

# 侧栏展示顺序 = 下面元组的顺序（先“实验控制”分区，再“数据与系统”分区）。
_PAGE_SPECS = (
    workbench.PAGE_SPEC,
    samples.PAGE_SPEC,
    scan.PAGE_SPEC,
    tuning.PAGE_SPEC,
    library.PAGE_SPEC,
    analysis.PAGE_SPEC,
    sync.PAGE_SPEC,
    settings.PAGE_SPEC,
)

for _spec in _PAGE_SPECS:
    register(_spec)
del _spec
del _PAGE_SPECS

__all__ = [
    "DEFAULT_SERVICE_URLS",
    "PageSpec",
    "PlaceholderPage",
    "SECTIONS",
    "ScanPage",
    "SystemSettingsPage",
    "TuningPage",
    "WorkbenchPage",
    "_valid_service_url",
    "page_specs",
    "register",
]
