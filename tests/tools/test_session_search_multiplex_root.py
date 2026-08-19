"""Tests for session_search's multiplex root-store merge and the
`profile=` cross-profile security guard (agentsmith #78).

Under gateway.multiplex_profiles, a routed Teams turn's HERMES_HOME is
context-overridden to the routed profile's home, so the profile's own
state.db only ever holds cli/cron sessions -- Teams conversations persist
in the gateway ROOT store instead (process env HERMES_HOME,
sessions.source='teams', keyed by sessions.user_id). These tests cover:

  1. browse/discover/read/scroll merging in the requester's own root-store
     Teams history, WITHOUT ever surfacing another user's root-store
     sessions.
  2. the `profile=` argument being refused under multiplex unless it names
     the caller's own current profile (the cross-profile security fix).
  3. multiplex OFF leaves existing single-store behaviour untouched.
"""
import json
import time

import pytest

import hermes_constants
from agent.secret_scope import set_multiplex_active
from gateway.session_context import reset_session_vars, set_session_vars
from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture(autouse=True)
def _reset_multiplex_state():
    """Multiplex flag + session contextvars are process/task globals — reset
    around every test so failures/success in one test can't bleed into the
    next."""
    set_multiplex_active(False)
    reset_session_vars()
    yield
    set_multiplex_active(False)
    reset_session_vars()


@pytest.fixture
def root_and_profile(tmp_path, monkeypatch):
    """Root state.db with two Teams users' sessions + a profile state.db
    with one cron session. HERMES_HOME (process env) points at the root;
    the context-local override points at the profile -- exactly the shape
    a routed multiplex Teams turn runs under.
    """
    root_home = tmp_path / "root"
    profile_home = tmp_path / "profiles" / "acmecorp"
    root_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)

    now = int(time.time())

    root_db = SessionDB(root_home / "state.db")
    root_db.create_session("teams_a1", source="teams", user_id="userA")
    root_db._conn.execute(
        "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
        (now - 500, "Q3 rollout plan", "teams_a1"),
    )
    root_db.append_message("teams_a1", role="user", content="Let's build the Q3 rollout plan")
    root_db.append_message("teams_a1", role="assistant", content="Starting the Q3 rollout plan doc.")

    root_db.create_session("teams_b1", source="teams", user_id="userB")
    root_db._conn.execute(
        "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
        (now - 400, "Confidential budget", "teams_b1"),
    )
    root_db.append_message("teams_b1", role="user", content="Let's discuss the confidential rollout plan budget")
    root_db.append_message("teams_b1", role="assistant", content="Noted, confidential rollout plan budget drafted.")
    root_db._conn.commit()
    root_db.close()

    profile_db = SessionDB(profile_home / "state.db")
    profile_db.create_session("cron_1", source="cron")
    profile_db._conn.execute(
        "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
        (now - 100, "Nightly rollup", "cron_1"),
    )
    profile_db.append_message("cron_1", role="user", content="run the nightly job")
    profile_db.append_message("cron_1", role="assistant", content="Nightly job complete.")
    profile_db._conn.commit()

    monkeypatch.setenv("HERMES_HOME", str(root_home))
    token = hermes_constants.set_hermes_home_override(str(profile_home))

    yield {"root_home": root_home, "profile_home": profile_home, "profile_db": profile_db}

    hermes_constants.reset_hermes_home_override(token)
    profile_db.close()


def _bind_teams_turn(user_id="userA", profile="acmecorp"):
    return set_session_vars(platform="teams", user_id=user_id, profile=profile)


class TestRootStoreMerge:
    def test_browse_includes_own_teams_and_profile_cron_never_other_user(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA")
        try:
            result = json.loads(session_search(db=root_and_profile["profile_db"]))
        finally:
            reset_session_vars()

        assert result["success"] is True
        sids = {r["session_id"] for r in result["results"]}
        assert "teams_a1" in sids
        assert "cron_1" in sids
        assert "teams_b1" not in sids

    def test_discover_query_never_surfaces_other_users_session(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA")
        try:
            result = json.loads(
                session_search(db=root_and_profile["profile_db"], query="rollout plan")
            )
        finally:
            reset_session_vars()

        assert result["success"] is True
        sids = {r["session_id"] for r in result["results"]}
        # userB's session ALSO matches "rollout plan" textually -- proves the
        # exclusion is the user_id filter, not a lack of a textual match.
        assert "teams_b1" not in sids
        assert "teams_a1" in sids

    def test_read_by_id_finds_own_root_session(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA")
        try:
            result = json.loads(
                session_search(db=root_and_profile["profile_db"], session_id="teams_a1")
            )
        finally:
            reset_session_vars()

        assert result["success"] is True
        assert result["session_id"] == "teams_a1"

    def test_read_by_id_refuses_other_users_root_session(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA")
        try:
            result = json.loads(
                session_search(db=root_and_profile["profile_db"], session_id="teams_b1")
            )
        finally:
            reset_session_vars()

        # Not found in the profile db, and root lookup refuses because
        # teams_b1's user_id ("userB") doesn't match the requester ("userA").
        assert result.get("success") is not True

    def test_scroll_refuses_other_users_root_session(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA")
        profile_db = root_and_profile["profile_db"]
        # Find userB's message id from the root store directly (out of band).
        import hermes_constants as hc
        prev = hc.get_hermes_home_override()
        root_db = SessionDB(root_and_profile["root_home"] / "state.db", read_only=True)
        b_msg_id = root_db.get_messages("teams_b1")[0]["id"]
        root_db.close()
        try:
            result = json.loads(
                session_search(
                    db=profile_db,
                    session_id="teams_b1",
                    around_message_id=b_msg_id,
                )
            )
        finally:
            reset_session_vars()

        assert result.get("success") is not True

    def test_multiplex_off_no_root_merge(self, root_and_profile):
        # Multiplex OFF entirely: no root-store merge should be attempted,
        # even though the same HERMES_HOME/override shape is in place.
        _bind_teams_turn(user_id="userA")
        try:
            result = json.loads(session_search(db=root_and_profile["profile_db"]))
        finally:
            reset_session_vars()

        assert result["success"] is True
        sids = {r["session_id"] for r in result["results"]}
        assert sids == {"cron_1"}


class TestCrossProfileGuard:
    def test_other_profile_blocked_under_multiplex(self, root_and_profile):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA", profile="acmecorp")
        try:
            result = json.loads(
                session_search(db=root_and_profile["profile_db"], profile="someoneelses")
            )
        finally:
            reset_session_vars()

        assert "error" in result
        assert "cross-profile" in result["error"]
        assert result.get("success") is False

    def test_same_profile_not_blocked_under_multiplex(self, root_and_profile, monkeypatch):
        set_multiplex_active(True)
        _bind_teams_turn(user_id="userA", profile="acmecorp")

        calls = []

        def _fake_resolve_profile_db(profile):
            calls.append(profile)
            return None  # falls back to caller's db unchanged

        monkeypatch.setattr(
            "tools.session_search_tool._resolve_profile_db", _fake_resolve_profile_db
        )
        try:
            result = json.loads(
                session_search(db=root_and_profile["profile_db"], profile="acmecorp")
            )
        finally:
            reset_session_vars()

        # The guard let it through to _resolve_profile_db instead of
        # short-circuiting with a tool_error.
        assert calls == ["acmecorp"]
        assert "error" not in result

    def test_cross_profile_allowed_when_multiplex_off(self, root_and_profile, monkeypatch):
        # Multiplex off: profile= reading another profile is intentional,
        # documented behaviour and must not be blocked by the new guard.
        calls = []

        def _fake_resolve_profile_db(profile):
            calls.append(profile)
            return None

        monkeypatch.setattr(
            "tools.session_search_tool._resolve_profile_db", _fake_resolve_profile_db
        )
        result = json.loads(
            session_search(db=root_and_profile["profile_db"], profile="someoneelses")
        )
        assert calls == ["someoneelses"]
        assert "error" not in result
