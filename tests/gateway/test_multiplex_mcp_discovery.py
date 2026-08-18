"""Multiplex gateways must register every served profile's MCP servers.

Startup discovery runs once in the default scope; secondary profiles'
``mcp_servers`` (e.g. per-user connectors) were only ever registered as a
side effect of cron, and per-session agents snapshot the tool registry at
construction — so routed inbound turns never saw ``mcp__<server>__*`` tools.
``_discover_multiplex_profile_mcp_tools`` closes that gap.
"""
from pathlib import Path
from types import SimpleNamespace

import gateway.run as run_mod
from hermes_constants import get_hermes_home


class TestMultiplexProfileMcpDiscovery:
    def test_noop_when_multiplex_off(self, monkeypatch):
        calls = []
        monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: calls.append(1))
        run_mod._discover_multiplex_profile_mcp_tools(SimpleNamespace(multiplex_profiles=False))
        assert calls == []

    def test_discovers_under_each_served_profile_scope(self, monkeypatch, tmp_path):
        root = tmp_path / "root"
        kyle = tmp_path / "profiles" / "kyle"
        jenny = tmp_path / "profiles" / "jenny"
        for d in (root, kyle, jenny):
            d.mkdir(parents=True)

        seen_homes = []

        def fake_discover():
            seen_homes.append(Path(get_hermes_home()).resolve())
            return []

        monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", fake_discover)
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex: [("default", root), ("kyle", kyle), ("jenny", jenny)],
        )
        # The scope helper hydrates profile secrets; keep the test hermetic.
        monkeypatch.setattr(
            "hermes_cli.env_loader.hydrate_profile_secret_sources", lambda home: None
        )

        run_mod._discover_multiplex_profile_mcp_tools(SimpleNamespace(multiplex_profiles=True))

        assert seen_homes == [root.resolve(), kyle.resolve(), jenny.resolve()]
        # Scope is torn down between profiles and after the last one.
        assert Path(get_hermes_home()).resolve() not in {kyle.resolve(), jenny.resolve()}

    def test_one_profile_failure_does_not_abort_the_rest(self, monkeypatch, tmp_path):
        homes = []
        a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()

        def fake_discover():
            home = Path(get_hermes_home()).resolve()
            homes.append(home)
            if home == a.resolve():
                raise RuntimeError("connector down")
            return []

        monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", fake_discover)
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve", lambda multiplex: [("a", a), ("b", b)]
        )
        monkeypatch.setattr(
            "hermes_cli.env_loader.hydrate_profile_secret_sources", lambda home: None
        )
        run_mod._discover_multiplex_profile_mcp_tools(SimpleNamespace(multiplex_profiles=True))
        assert homes == [a.resolve(), b.resolve()]
