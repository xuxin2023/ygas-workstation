"""Configurable protocol profiles for bench differences."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtocolProfile:
    name: str
    display_name: str
    description: str
    average_channel_mapping: dict[str, str]

    def channel_name(self, average_code: str) -> str:
        return self.average_channel_mapping.get(average_code.upper(), average_code.upper())


PROFILES: dict[str, ProtocolProfile] = {
    "bench_default": ProtocolProfile(
        name="bench_default",
        display_name="bench_default（现场固定）",
        description="现场 bench 固定规则：AVERAGE1 = 水 = H2O，AVERAGE2 = 气 = CO2。",
        average_channel_mapping={"AVERAGE1": "H2O", "AVERAGE2": "CO2"},
    ),
    "manual_default": ProtocolProfile(
        name="manual_default",
        display_name="manual_default（内部兼容）",
        description="仅保留给兼容场景，常规界面不展示；主界面统一按现场 bench 规则解释 AVERAGE1/AVERAGE2。",
        average_channel_mapping={"AVERAGE1": "CO2", "AVERAGE2": "H2O"},
    ),
}


def get_profile(name: str) -> ProtocolProfile:
    return PROFILES.get(name, PROFILES["bench_default"])


def profile_names() -> list[str]:
    return list(PROFILES)
