from __future__ import annotations

from p2p_agents.connectors.base import Connector
from p2p_agents.connectors.file_bundle import FileBundleConnector
from p2p_agents.connectors.rest import RestConnector
from p2p_agents.settings import InstanceConfig, PlatformConfig


def make_connector(platform: PlatformConfig, instance: InstanceConfig) -> Connector:
    if platform.connector == "rest":
        return RestConnector(platform, instance)
    return FileBundleConnector(platform, instance)


__all__ = ["Connector", "FileBundleConnector", "RestConnector", "make_connector"]
