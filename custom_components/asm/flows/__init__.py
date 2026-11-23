"""The providers' parts of the config and options flow, one module each (see base.py).

A new provider adds its module, its ``ProviderFlow`` to PROVIDER_FLOWS, and the
mixins of steps of its own to CONFIG_FLOW_MIXINS or OPTIONS_FLOW_MIXINS.
"""
from __future__ import annotations

from .awattar import AwattarFlow
from .base import ProviderFlow
from .dsmr import DsmrFlow
from .energiedaten import EnergiedatenFlow
from .energylive import EnergyliveFlow
from .ewerk_goesting import EwerkGoestingFlow
from .netz_noe import NetzNoeFlow
from .salzburgnetz import (
    SalzburgNetzConfigFlowMixin,
    SalzburgNetzFlow,
    SalzburgNetzOptionsFlowMixin,
)
from .selectra import SelectraConfigFlowMixin, SelectraFlow
from .wiener_netze import WienerNetzeFlow

PROVIDER_FLOWS: dict[str, ProviderFlow] = {
    flow.provider: flow
    for flow in (
        WienerNetzeFlow(),
        NetzNoeFlow(),
        EnergiedatenFlow(),
        EnergyliveFlow(),
        DsmrFlow(),
        AwattarFlow(),
        SelectraFlow(),
        SalzburgNetzFlow(),
        EwerkGoestingFlow(),
    )
}

# The steps providers add to the config flow and to the options flow.
CONFIG_FLOW_MIXINS = (SelectraConfigFlowMixin, SalzburgNetzConfigFlowMixin)
OPTIONS_FLOW_MIXINS = (SalzburgNetzOptionsFlowMixin,)


def provider_flow(provider: str) -> ProviderFlow:
    """Return a provider's part of the flows; a portal login if it has none."""
    if (flow := PROVIDER_FLOWS.get(provider)) is not None:
        return flow
    flow = ProviderFlow()
    flow.provider = provider
    return flow


__all__ = [
    "CONFIG_FLOW_MIXINS",
    "OPTIONS_FLOW_MIXINS",
    "PROVIDER_FLOWS",
    "ProviderFlow",
    "provider_flow",
]
