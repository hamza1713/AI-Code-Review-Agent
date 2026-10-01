"""
Interactive PR Bot Package.
Provides slash command routing, PR description generation, in-thread Q&A, and policy compliance checks.
"""

from .command_router import BotCommandResult, CommandRouter

__all__ = [
    "CommandRouter",
    "BotCommandResult",
]
