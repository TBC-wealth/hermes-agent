"""Regression tests for the _find_all_skills discovery cache (#58985 salvage).

Covers the cache-signature fix layered on the cherry-picked contributor
commit: the original keyed the cache on the max mtime of only the TOP-LEVEL
scan dirs, so adding/removing a skill inside a category subdir (which bumps
the category dir's mtime, not the root's) served a stale list indefinitely.
The signature now covers roots + immediate children (mirroring
hermes_cli/profiles.py::_count_skills) plus the disabled-set, with a short
TTL bounding in-place SKILL.md edit staleness.
"""

import json
import time

import pytest

import tools.skills_tool as st


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch, tmp_path):
    """Isolate every test: clear the module cache and point the scan at
    an empty external-dirs list + a tmp skills root."""
    st._SKILLS_CACHE.clear()
    monkeypatch.setattr(st, "_skills_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs", lambda: []
    )
    monkeypatch.setattr(st, "_get_disabled_skill_names", lambda: set())
    yield
    st._SKILLS_CACHE.clear()


def _write_skill(root, category, name, description="a skill"):
    d = root / "skills" / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return d


def test_cache_hit_serves_copies_not_cache_objects(tmp_path):
    """Callers mutate the returned dicts (web_server annotates
    s['enabled']/s['usage']) — the cache must hand out per-call copies."""
    _write_skill(tmp_path, "cat-a", "skill-one")
    first = st._find_all_skills()
    assert [s["name"] for s in first] == ["skill-one"]

    # Mutate what the first caller got; the next (cached) call must be clean.
    first[0]["enabled"] = False
    first.append({"name": "junk"})

    second = st._find_all_skills()
    assert [s["name"] for s in second] == ["skill-one"]
    assert "enabled" not in second[0], "cache poisoned by caller mutation"
    assert second is not first


def test_disabled_and_full_views_cached_separately(tmp_path, monkeypatch):
    _write_skill(tmp_path, "cat-a", "skill-one")
    _write_skill(tmp_path, "cat-a", "skill-two")
    monkeypatch.setattr(st, "_get_disabled_skill_names", lambda: {"skill-two"})

    filtered = sorted(s["name"] for s in st._find_all_skills())
    everything = sorted(s["name"] for s in st._find_all_skills(skip_disabled=True))
    assert filtered == ["skill-one"]
    assert everything == ["skill-one", "skill-two"]


def test_dot_disabled_skill_directories_are_not_discovered(tmp_path):
    _write_skill(tmp_path, "cat-a", "active-skill")
    _write_skill(tmp_path, "cat-a", "archived-skill.disabled")

    discovered = st._find_all_skills(skip_disabled=True)

    assert [skill["name"] for skill in discovered] == ["active-skill"]


def test_dot_disabled_skill_does_not_collide_with_active_external_skill(
    tmp_path, monkeypatch
):
    archived = _write_skill(tmp_path, "cat-a", "shared.disabled")
    (archived / "SKILL.md").write_text(
        "---\nname: shared\ndescription: archived\n---\n# archived\n",
        encoding="utf-8",
    )
    external_root = tmp_path / "external"
    active = external_root / "shared"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text(
        "---\nname: shared\ndescription: active\n---\n# active\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs", lambda: [external_root]
    )

    viewed = json.loads(st.skill_view("shared"))

    assert viewed["success"] is True
    assert viewed["description"] == "active"
