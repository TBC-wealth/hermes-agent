import json
import stat

from gateway.run import _claim_agentsmith_profile_welcome


def test_profile_welcome_is_claimed_once_with_private_state(tmp_path):
    profile = tmp_path / "profiles" / "kyle"
    profile.mkdir(parents=True)

    assert _claim_agentsmith_profile_welcome(profile) is True
    flag = profile / "state" / "agentsmith-welcome-v1.json"
    assert json.loads(flag.read_text()) == {"format": 1, "claimed": True}
    assert stat.S_IMODE(flag.stat().st_mode) == 0o600
    assert stat.S_IMODE(flag.parent.stat().st_mode) == 0o700
    assert _claim_agentsmith_profile_welcome(profile) is False


def test_profile_welcome_rejects_a_symlinked_state_directory(tmp_path):
    profile = tmp_path / "profiles" / "kyle"
    target = tmp_path / "outside"
    profile.mkdir(parents=True)
    target.mkdir()
    (profile / "state").symlink_to(target, target_is_directory=True)

    assert _claim_agentsmith_profile_welcome(profile) is False
    assert not (target / "agentsmith-welcome-v1.json").exists()


def test_profile_welcome_claim_works_without_posix_euid(tmp_path, monkeypatch):
    monkeypatch.delattr("gateway.run.os.geteuid")

    assert _claim_agentsmith_profile_welcome(tmp_path / "windows-profile") is True
