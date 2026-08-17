import threading
from types import SimpleNamespace


def test_skills_prompt_uses_explicit_profile_home(tmp_path, monkeypatch):
    from agent import prompt_builder

    default_home = tmp_path / "default"
    leaky = default_home / "skills/general/leaky-skill"
    leaky.mkdir(parents=True)
    (leaky / "SKILL.md").write_text(
        "---\nname: leaky-skill\ndescription: must not leak\n---\nbody\n"
    )
    profile_skills = tmp_path / "profiles/alberto/skills"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=False)
    result = {}

    def build():
        result["prompt"] = prompt_builder.build_skills_system_prompt(
            skills_dir_override=profile_skills
        )

    thread = threading.Thread(target=build)
    thread.start()
    thread.join()
    assert result["prompt"] == ""


def test_soul_uses_explicit_profile_home(tmp_path, monkeypatch):
    from agent.prompt_builder import load_soul_md

    default_home = tmp_path / "default"
    default_home.mkdir()
    (default_home / "SOUL.md").write_text("DEFAULT SOUL")
    profile_home = tmp_path / "profiles/alberto"
    profile_home.mkdir(parents=True)
    (profile_home / "SOUL.md").write_text("ALBERTO SOUL")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    assert load_soul_md(home_override=profile_home) == "ALBERTO SOUL"


def test_bound_override_wins_over_shared_session_database(tmp_path, monkeypatch):
    from agent.system_prompt import _agent_home, _profile_name_for_home
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = tmp_path / "root"
    profile_home = root / "profiles/alberto"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    agent = SimpleNamespace(
        _session_db=SimpleNamespace(db_path=root / "state.db")
    )
    token = set_hermes_home_override(profile_home)
    try:
        assert _agent_home(agent) == profile_home
        assert _profile_name_for_home(profile_home) == "alberto"
    finally:
        reset_hermes_home_override(token)
