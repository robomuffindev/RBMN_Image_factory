"""Robomuffin Image Factory — Python SDK.

Re-export the client from the top of the package so embedders can simply::

    from robomuffin_client import RobomuffinClient
"""
from .robomuffin_client import RobomuffinClient, RobomuffinError

__all__ = ["RobomuffinClient", "RobomuffinError"]
