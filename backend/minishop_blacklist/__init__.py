"""Minishop Plugin API v1 entry point."""

from pathlib import Path

from aiohttp import web
from bot.plugins.extensions import ExtensionContributions
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext, WorkerTaskSpec
from db.migrator import Migration

from .alerts import PLUGIN_ID, alert_contributions, subscribe_to_events
from .api import setup_routes
from .diagnostic_api import setup_diagnostic_routes
from .migrations import migrations
from .worker import run_refresh_worker

__version__ = "0.2.0"


class BlacklistPlugin(Plugin):
    name = PLUGIN_ID
    version = __version__
    plugin_api_min_version = 1
    plugin_api_max_version = 1

    def setup(self, ctx: PluginContext) -> None:
        if ctx.session_factory is not None:
            subscribe_to_events(ctx)

    def extensions(self, ctx: PluginContext) -> ExtensionContributions:
        return alert_contributions()

    def migrations(self) -> list[Migration]:
        return migrations()

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope == WEB_SCOPE_WEBAPP:
            setup_routes(app)
            setup_diagnostic_routes(app)

    def locales_dir(self) -> Path:
        packaged = Path(__file__).parent / "locales"
        return packaged if packaged.is_dir() else Path(__file__).parents[2] / "locales"

    def worker_tasks(self, ctx: PluginContext) -> list[WorkerTaskSpec]:
        return [WorkerTaskSpec(name=f"{self.name}.refresh", factory=run_refresh_worker)]


plugin = BlacklistPlugin()
