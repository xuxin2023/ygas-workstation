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
        display_name="Bench Default",
        description="现场 bench 经验：AVERAGE1 对应 H2O，AVERAGE2 对应 CO2。",
        average_channel_mapping={"AVERAGE1": "H2O", "AVERAGE2": "CO2"},
    ),
    "manual_default": ProtocolProfile(
        name="manual_default",
        display_name="Manual Default",
        description="按手册解释：AVERAGE1 对应 CO2，AVERAGE2 对应 H2O。",
        average_channel_mapping={"AVERAGE1": "CO2", "AVERAGE2": "H2O"},
    ),
}


def get_profile(name: str) -> ProtocolProfile:
    return PROFILES.get(name, PROFILES["bench_default"])


def profile_names() -> list[str]:
    return list(PROFILES)
