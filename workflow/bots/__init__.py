"""Bots — Handlers de Azure Bot Framework para canal y personal."""

from bots.channel_bot import DappsChannelBot, build_channel_conversation_reference
from bots.personal_bot import DappsPersonalBot

__all__ = [
    "DappsChannelBot",
    "DappsPersonalBot",
    "build_channel_conversation_reference",
]
