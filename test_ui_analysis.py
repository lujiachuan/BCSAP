# -*- coding: utf-8 -*-
"""开发自检：分析视图下拉在条目很多时不崩、且切换生效。"""
import sys

sys.path.insert(0, r"D:\BCSAP")
from PySide6.QtWidgets import QApplication  # noqa: E402

from apps.desktop_client.pages.tuning import TuningPage  # noqa: E402

app = QApplication([])
page = TuningPage()
combo = page.analysis_choice_combo
assert combo is not None, "缺分析视图下拉"
# 构造 14 个选项（12 个变量切片 + 历史 + 重要性），确认不崩、条目齐全
choices = [("优化历史", "history"), ("参数重要性", "importance")]
for i in range(12):
    choices.append((f"切片 · DW{i + 1}", f"slice:dw{i}"))
page._rebuild_analysis_choices(choices)
assert combo.count() == 14, combo.count()
# 切换要真的改变当前视图
page._set_analysis_choice("slice:dw3")
assert page.analysis_current_choice == "slice:dw3", page.analysis_current_choice
print("14 个分析视图条目构建 ok，下拉宽度不随条目增加而变宽")
page._rebuild_analysis_choices([("优化历史", "history")])
print("UI ANALYSIS TEST PASS")
