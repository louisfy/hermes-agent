"""Profile creation respects the caller's persistent wrapper preference (#142951)."""

import argparse
import os
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import gateway_multiplex_served, profile_cmd, profiles
from hermes_cli.config import atomic_config_write
from hermes_cli.subcommands.profile import build_profile_parser


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.setenv("PATH", str(tmp_path / ".local" / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    # Creation and wrapper I/O stay real; no skill sync or running gateway is needed.
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda *_: {"skipped_opt_out": True})
    monkeypatch.setattr(profiles, "_maybe_register_gateway_service", lambda *_: None)
    monkeypatch.setattr(profiles, "_notify_multiplexer", lambda *_: None)
    monkeypatch.setattr(gateway_multiplex_served, "live_default_gateway_pid", lambda: None)
    return home


def create(name, *options, no_skills=True):
    parser = argparse.ArgumentParser(prog="hermes")
    build_profile_parser(parser.add_subparsers(), cmd_profile=profile_cmd.cmd_profile)
    skill_options = ["--no-skills"] if no_skills else []
    args = parser.parse_args(["profile", "create", name, *skill_options, *options])
    args.func(args)


@pytest.mark.parametrize("setting", [None, True, False])
def test_create_uses_saved_preference_and_keeps_default(profile_home, capsys, setting):
    if setting is not None:
        atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": setting}})

    create("research")

    assert (profile_home / "profiles" / "research" / ".env").is_file()
    wrapper = profiles._wrapper_path("research")
    assert wrapper.exists() is (setting is not False)
    output = capsys.readouterr().out
    command = "hermes -p research" if setting is False else "research"
    assert f"  {command} setup" in output
    assert f"  {command} chat" in output
    assert f"  {command} gateway start" in output
    if setting is None:
        assert not (profile_home / "config.yaml").exists()


@pytest.mark.parametrize("setting", [True, False])
def test_no_alias_flag_still_wins(profile_home, setting):
    atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": setting}})

    create("research", "--no-alias")

    assert not profiles._wrapper_path("research").exists()


def test_opt_out_preserves_existing_wrappers_and_explicit_alias_creation(profile_home):
    atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": False}})
    existing = profiles.create_wrapper_script("keep-existing")
    before = existing.read_bytes()

    create("research")

    assert existing.read_bytes() == before
    assert not profiles._wrapper_path("research").exists()
    profile_cmd._profile_alias(argparse.Namespace(profile_name="research", remove=False))
    assert profiles._wrapper_path("research").is_file()


def test_creation_uses_invoking_profile_not_default_or_new_profile(profile_home, monkeypatch):
    atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": True}})
    caller = profile_home / "profiles" / "caller"
    caller.mkdir(parents=True)
    atomic_config_write(caller / "config.yaml", {"profiles": {"auto_alias": False}})
    monkeypatch.setenv("HERMES_HOME", str(caller))

    create("research")

    assert (profile_home / "profiles" / "research" / ".env").is_file()
    assert not profiles._wrapper_path("research").exists()


def test_managed_overlay_can_disable_automatic_aliases(profile_home):
    atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": True}})
    managed = profile_home.parent / "managed"
    managed.mkdir()
    atomic_config_write(managed / "config.yaml", {"profiles": {"auto_alias": False}})

    create("research")

    assert not profiles._wrapper_path("research").exists()


def test_clone_uses_callers_preference_not_cloned_config(profile_home):
    atomic_config_write(profile_home / "config.yaml", {"profiles": {"auto_alias": False}})
    source = profile_home / "profiles" / "source"
    source.mkdir(parents=True)
    atomic_config_write(source / "config.yaml", {"profiles": {"auto_alias": True}})

    create("research", "--clone-from", "source", no_skills=False)

    assert (profile_home / "profiles" / "research" / "config.yaml").is_file()
    assert not profiles._wrapper_path("research").exists()


@pytest.mark.parametrize("section", [None, "invalid", {"auto_alias": "false"}, {"auto_alias": 0}])
def test_only_boolean_false_disables_the_default(profile_home, section):
    atomic_config_write(profile_home / "config.yaml", {"profiles": section})

    create("research")

    assert profiles._wrapper_path("research").is_file()


def test_command_collision_never_overwrites_an_existing_file(profile_home, capsys):
    wrapper = profiles._wrapper_path("research")
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("foreign command\n", encoding="utf-8")
    wrapper.chmod(0o755)

    create("research")

    assert wrapper.read_text(encoding="utf-8") == "foreign command\n"
    output = capsys.readouterr().out
    assert "Cannot create alias" in output
    assert "hermes -p research setup" in output
