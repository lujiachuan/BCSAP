"""手动控制主面板的展示配置。"""

from pydantic import BaseModel, ConfigDict


class ManualCard(BaseModel):
    """一张主面板卡片；只描述展示关系，不承载设备安全参数。"""

    model_config = ConfigDict(strict=True)

    card_id: str
    title: str
    device_ids: list[str]
    visible: bool = True
    display_order: int = 0
    system: bool = False


class ManualDashboardConfig(BaseModel):
    """手动控制主面板配置。"""

    model_config = ConfigDict(strict=True)

    version: int = 1
    cards: list[ManualCard]
