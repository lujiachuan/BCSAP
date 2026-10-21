"""手动控制设备库与主面板配置。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apps.instrument_service import manual_dashboard
from packages.contracts import ManualCard, ManualDashboardConfig, PvMappingConfig, PvMappingEntry


def mapping() -> PvMappingConfig:
    return PvMappingConfig(
        version=1,
        entries=[
            PvMappingEntry(
                signal="magnet.m1.current_setpoint",
                label="磁铁1 电流设定",
                pv="M1:ISET",
                unit="A",
                writable=True,
                required=True,
                group="磁铁电源",
                device_id="magnet.m1",
                role="setpoint",
            ),
            PvMappingEntry(
                signal="detector.fc1.beam_current",
                label="FC1 束流电流",
                pv="FC1:I",
                unit="nA",
                writable=False,
                required=True,
                group="束流探测",
                device_id="detector.fc1",
                role="readback",
            ),
            PvMappingEntry(
                signal="gas.ar.flow_setpoint",
                label="Ar 流量设定",
                pv="AR:FLOW",
                unit="sccm",
                writable=True,
                required=True,
                group="气体流量",
                device_id="gas.ar",
                role="setpoint",
            ),
        ],
    )


class ManualDashboardTests(unittest.TestCase):
    def test_default_cards_are_derived_from_devices(self) -> None:
        config = manual_dashboard.default_config(mapping())

        self.assertEqual(config.cards[0].card_id, manual_dashboard.TREND_CARD_ID)
        self.assertEqual(config.cards[1].device_ids, ["magnet.m1"])

    def test_device_cannot_belong_to_two_cards(self) -> None:
        config = manual_dashboard.default_config(mapping())
        config.cards.append(
            ManualCard(
                card_id="duplicate",
                title="重复",
                device_ids=["magnet.m1"],
            )
        )

        issues = manual_dashboard.validate_config(config, mapping())

        self.assertTrue(any("同时属于" in issue for issue in issues))

    def test_card_title_is_independent_from_mapping_group(self) -> None:
        config = manual_dashboard.default_config(mapping())
        config.cards[1].title = "我的磁铁"

        self.assertEqual(manual_dashboard.validate_config(config, mapping()), [])
        self.assertEqual(mapping().entries[0].group, "磁铁电源")

    def test_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.json"
            with mock.patch.dict(os.environ, {"SPECTRUM_MANUAL_DASHBOARD": str(path)}):
                config = manual_dashboard.default_config(mapping())
                config.cards[1].visible = False
                manual_dashboard.save_config(config, mapping())

                loaded = manual_dashboard.load_config(mapping())

        self.assertFalse(loaded.cards[1].visible)

    def test_removed_device_does_not_reset_other_card_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.json"
            with mock.patch.dict(os.environ, {"SPECTRUM_MANUAL_DASHBOARD": str(path)}):
                config = manual_dashboard.default_config(mapping())
                config.cards[2].title = "我的气路"
                manual_dashboard.save_config(config, mapping())
                reduced = mapping().model_copy(update={"entries": mapping().entries[1:]})

                loaded = manual_dashboard.load_config(reduced)

        self.assertEqual([card.title for card in loaded.cards], ["束流电流趋势", "我的气路"])

    def test_system_trend_card_cannot_be_removed(self) -> None:
        config = ManualDashboardConfig(version=1, cards=[])

        issues = manual_dashboard.validate_config(config, mapping())

        self.assertTrue(any("系统趋势卡片" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
