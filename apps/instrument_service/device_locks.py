"""设备组锁：按真实共享资源分组，由执行服务持有。

架构依据（文档 6.3）：

* 锁对象按**真实共享资源**分组（磁场电源、离子光学电源组、探测器……），
  而不是按界面模块；
* 扫谱与调束在启动前**一次性声明**需要的全部资源，拿不齐就不启动——
  先拿一半再等另一半会留下「半个任务占着设备」的中间状态；
* 锁由执行服务持有，**不由界面内存里的布尔变量代表**：界面重启不该释放设备锁；
* 任务结束释放软件锁**不等于**设备回到某个值，恢复动作由明确的设备策略决定。

锁只在本进程内有效（第一版执行服务是单机单进程）。服务重启后锁自然清空，
此时应按文档要求先核对设备实际状态，而不是认为设备空闲——所以
``release_all`` 只用于进程退出，不用于「恢复运行」。
"""

from __future__ import annotations

from collections.abc import Iterable
from threading import RLock


class DeviceBusy(RuntimeError):
    """请求的设备组正被其它任务占用。"""


class DeviceLockManager:
    """按分组名加锁，记录持有者，供诊断与冲突提示。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._held: dict[str, str] = {}

    def acquire(self, groups: Iterable[str], owner: str) -> None:
        """一次性获取全部分组。

        任一分组已被**别人**占用则一个都不获取并抛 ``DeviceBusy``；
        同一 owner 重复获取自己已持有的分组是幂等的（便于重入）。
        """
        wanted = {group for group in groups if group}
        if not wanted:
            return
        with self._lock:
            conflicts = {
                group: holder
                for group, holder in self._held.items()
                if group in wanted and holder != owner
            }
            if conflicts:
                detail = "、".join(
                    f"{group}（被 {holder} 占用）" for group, holder in conflicts.items()
                )
                raise DeviceBusy(f"设备组不可用：{detail}")
            for group in wanted:
                self._held[group] = owner

    def release(self, owner: str) -> None:
        """释放该 owner 持有的全部分组。"""
        with self._lock:
            for group in [g for g, holder in self._held.items() if holder == owner]:
                del self._held[group]

    def held(self) -> dict[str, str]:
        """当前占用情况（分组 → 持有者），供状态接口与诊断展示。"""
        with self._lock:
            return dict(self._held)

    def release_all(self) -> None:
        """清空全部锁（仅用于进程退出）。"""
        with self._lock:
            self._held.clear()
