"""与界面和基础设施无关的领域规则。"""

from .scan import InvalidScanTransition, ScanState, transition_scan

__all__ = ["InvalidScanTransition", "ScanState", "transition_scan"]

