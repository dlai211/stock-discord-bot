"""Entry point for the Discord finance bot.

Run with:  python bot.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config

LOG_FORMAT = "%(asctime)s  %(levelname)-8s %(name)-22s %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("bot")

COGS_DIR = Path(__file__).resolve().parent / "cogs"


def discover_extensions() -> list[str]:
    """Return every cog module in cogs/ as an importable extension name."""
    if not COGS_DIR.is_dir():
        return []
    return [
        f"cogs.{path.stem}"
        for path in sorted(COGS_DIR.glob("*.py"))
        if not path.stem.startswith("_")
    ]


async def _reply_to_interaction(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral error, whether or not the interaction was deferred."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver an error message to the user", exc_info=True)


class FinanceBot(commands.Bot):
    """Bot subclass that loads cogs and syncs the slash command tree on startup."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        extensions = discover_extensions()
        loaded: list[str] = []
        failed: list[str] = []

        for extension in extensions:
            try:
                await self.load_extension(extension)
                loaded.append(extension)
            except Exception:
                failed.append(extension)
                log.exception("Could not load extension %s", extension)

        if loaded:
            log.info("Loaded %d cog(s): %s", len(loaded), ", ".join(loaded))
        if failed:
            log.error("Failed to load %d cog(s): %s", len(failed), ", ".join(failed))

        # Synced once here rather than in on_ready: cogs have registered their
        # commands by this point, and on_ready fires again on every reconnect
        # while Discord rate-limits global syncs.
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s) globally", len(synced))
        except discord.HTTPException:
            log.exception("Global slash command sync failed; commands may be stale")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info(
            "Latency %.0f ms — connected to %d guild(s)",
            self.latency * 1000,
            len(self.guilds),
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        """Catch-all for prefix commands. Slash errors go to on_app_command_error."""
        if isinstance(error, commands.CommandNotFound):
            return  # Not worth replying to; people mistype constantly.

        if isinstance(error, commands.MissingPermissions):
            await ctx.reply(
                "You don't have permission to run that command.", mention_author=False
            )
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                f"Missing argument: `{error.param.name}`.", mention_author=False
            )
            return

        if isinstance(error, commands.CheckFailure):
            await ctx.reply("That command isn't available here.", mention_author=False)
            return

        log.error("Unhandled error in prefix command %s", ctx.command, exc_info=error)
        await ctx.reply(
            "Something went wrong handling that command — it's been logged.",
            mention_author=False,
        )

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Catch-all for slash commands."""
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original  # unwrap to the exception the command actually raised

        if isinstance(error, app_commands.MissingPermissions):
            message = "You don't have permission to use that command."
        elif isinstance(error, app_commands.CheckFailure):
            message = "That command isn't available here."
        else:
            log.error(
                "Unhandled error in slash command %s",
                getattr(interaction.command, "qualified_name", "?"),
                exc_info=error,
            )
            message = "Something went wrong running that command — it's been logged."

        await _reply_to_interaction(interaction, message)


def main() -> int:
    missing = config.missing_variables()
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        log.error("Add them to %s — see .env.example for the format.", config.ENV_PATH)
        return 1

    bot = FinanceBot()
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token. Re-check DISCORD_TOKEN in .env.")
        return 1
    except KeyboardInterrupt:
        log.info("Shutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
