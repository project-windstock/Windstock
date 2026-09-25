"""
Windstock -- a from-scratch private server for Pokemon GO 0.29 (July 2016).

The server is organised as a conventional application package so each concern
lives in its own layer:

    config/   settings, live events, placed world objects, filesystem paths
    game/     protocol builders, world/player state, RPC, shop, spawn data
    geo/      biome + OpenStreetMap point-of-interest data sources
    net/      TLS game server, DNS redirector, PTC SSO
    web/      World Manager (admin), help center, shop site, downloads UI
    cli/      the interactive slash-command console
    ui/       the desktop launcher window
    tools/    standalone developer utilities (certs, POI fetchers, logins)

Import the entry point lazily; importing :mod:`windstock` must stay cheap and
free of side effects (it may be imported while a running server reloads a
submodule).
"""

__version__ = "0.29.0"
__all__ = ["__version__"]
