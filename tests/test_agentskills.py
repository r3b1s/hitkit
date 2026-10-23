"""Agent skills: valid per the Agent Skills spec, consistent with the CLI, installable."""

from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from hitkit import agentskills, cli
from hitkit.errors import Conflict

SKILLS = agentskills.available()
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def test_hitkit_ships_skills():
    assert {s.name for s in SKILLS} >= {"hitkit", "hitkit-forge", "hitkit-troubleshoot"}


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_skill_frontmatter_follows_the_spec(skill):
    text = (skill.path / "SKILL.md").read_text()
    meta = agentskills.frontmatter(text)

    assert NAME.match(skill.name) and len(skill.name) <= 64
    assert skill.name == skill.path.name
    assert 0 < len(skill.description) <= 1024
    assert len(meta.get("compatibility", "")) <= 500
    assert len(text.splitlines()) < 500


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_relative_links_resolve(skill):
    for target in re.findall(r"\]\(([^)#:]+)\)", (skill.path / "SKILL.md").read_text()):
        assert (skill.path / target).is_file(), target


def _commands_in(text: str):
    """`hitkit <cmd> [<sub>]` where code invokes it: at the start of a line or code span,
    or after `sudo `, `$(`, a pipe, or an environment assignment."""
    code = "\n".join(re.findall(r"```.*?```", text, re.S) + re.findall(r"`([^`\n]+)`", text))
    pattern = r"(?m)(?:^\s*|sudo |\$\(|\| |^[A-Z_]+=\S+ )hitkit((?:[ \t]+[a-z][a-z-]*)+)"
    for m in re.finditer(pattern, code):
        yield m.group(1).split()


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_every_documented_command_exists(skill):
    texts = [p.read_text() for p in skill.path.rglob("*.md")]
    for text in texts:
        for words in _commands_in(text):
            cmd = cli.cli.commands.get(words[0])
            assert cmd is not None, f"hitkit {' '.join(words)}"
            if isinstance(cmd, click.Group) and len(words) > 1:
                assert words[1] in cmd.commands, f"hitkit {' '.join(words)}"


def test_install_copies_and_refreshes_its_own_skills(tmp_path):
    installed = agentskills.install(tmp_path)
    (tmp_path / "hitkit" / "stale.md").write_text("old")

    agentskills.install(tmp_path)

    assert {p.name for p in installed} == {s.name for s in SKILLS}
    assert (tmp_path / "hitkit-forge" / "SKILL.md").is_file()
    assert not (tmp_path / "hitkit" / "stale.md").exists()


def test_install_refuses_to_replace_someone_elses_directory(tmp_path):
    (tmp_path / "hitkit").mkdir()
    (tmp_path / "hitkit" / "notes.txt").write_text("mine")

    with pytest.raises(Conflict):
        agentskills.install(tmp_path)
    assert (tmp_path / "hitkit" / "notes.txt").exists()


def test_skills_live_in_the_repository_root_skills_directory():
    root = agentskills.SKILLS_DIR
    assert root.name == "skills" and (root / "hitkit" / "SKILL.md").is_file()


def test_wheel_bundles_the_root_skills_directory():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    force = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force == {"skills": "hitkit/skills"}


@pytest.fixture
def cli_user(user, monkeypatch):
    monkeypatch.setattr(cli, "current_user", lambda: user)


def test_skills_command_lists(cli_user):
    listing = CliRunner().invoke(cli.cli, ["skills", "--json"])

    assert listing.exit_code == 0 and "hitkit-troubleshoot" in listing.output


def test_skills_install_copies_into_a_given_directory(cli_user, tmp_path):
    result = CliRunner().invoke(cli.cli, ["skills", "install", str(tmp_path / "skills")])

    assert result.exit_code == 0
    assert (tmp_path / "skills" / "hitkit" / "references" / "kits.md").is_file()


def test_skills_install_defaults_to_agents_skills_in_the_working_directory(cli_user, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agents").mkdir()

    result = CliRunner().invoke(cli.cli, ["skills", "install"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".agents" / "skills" / "hitkit" / "SKILL.md").is_file()


def test_skills_install_asks_before_creating_agents_and_creates_it_on_yes(cli_user, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["skills", "install"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Create it?" in result.output
    assert (tmp_path / ".agents" / "skills" / "hitkit-forge" / "SKILL.md").is_file()


def test_skills_install_accepts_the_default_answer_to_the_prompt(cli_user, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["skills", "install"], input="\n")

    assert result.exit_code == 0 and (tmp_path / ".agents" / "skills").is_dir()


def test_skills_install_installs_nothing_when_the_prompt_is_declined(cli_user, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["skills", "install"], input="n\n")

    assert result.exit_code == 1
    assert not (tmp_path / ".agents").exists()


def test_skills_install_yes_skips_the_prompt(cli_user, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["skills", "install", "--yes"])

    assert result.exit_code == 0 and "Create it?" not in result.output
    assert (tmp_path / ".agents" / "skills" / "hitkit" / "SKILL.md").is_file()


def test_the_old_install_flag_is_gone(cli_user, tmp_path):
    assert CliRunner().invoke(cli.cli, ["skills", "--install", str(tmp_path)]).exit_code == 2


def test_skills_print_shows_the_skill_md(cli_user):
    result = CliRunner().invoke(cli.cli, ["skills", "print", "hitkit-forge"])

    assert result.exit_code == 0
    assert result.output == (agentskills.find("hitkit-forge").path / "SKILL.md").read_text()


def test_skills_print_unknown_name_lists_the_available_ones(cli_user):
    result = CliRunner().invoke(cli.cli, ["skills", "print", "nope"])

    assert result.exit_code == 1
    assert "no bundled skill named 'nope'" in result.output and "hitkit-forge" in result.output


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_mentioned_scripts_exist_and_are_executable(skill):
    import os

    for script in set(re.findall(r"scripts/[\w.-]+\.py", (skill.path / "SKILL.md").read_text())):
        assert os.access(skill.path / script, os.X_OK), script


@pytest.fixture
def forge_api(monkeypatch):
    import importlib.util

    path = next(s.path for s in SKILLS if s.name == "hitkit-forge") / "scripts" / "forge_api.py"
    spec = importlib.util.spec_from_file_location("forge_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_TOKEN_FILE", raising=False)
    return module


def test_forge_api_reads_the_token_create_json_or_a_bare_token(forge_api, tmp_path, monkeypatch):
    token_file = tmp_path / "t.json"
    token_file.write_text('{"name": "agent-x", "token": "abc123", "scopes": [], "user": "t"}\n')
    monkeypatch.setenv("FORGE_TOKEN_FILE", str(token_file))
    assert forge_api._token() == "abc123"

    token_file.write_text("bare-token\n")
    assert forge_api._token() == "bare-token"


def test_forge_api_sends_the_token_as_a_header_and_maps_http_errors(forge_api, monkeypatch, capsys):
    import io
    import urllib.error

    monkeypatch.setenv("FORGE_TOKEN", "abc123")
    seen = []

    def fake_urlopen(req, context=None, timeout=None):
        seen.append(req)
        raise urllib.error.HTTPError(req.full_url, 404, "nf", None, io.BytesIO(b'{"message":"nope"}'))

    monkeypatch.setattr(forge_api.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(forge_api, "_ca", lambda: None)  # don't depend on this host's hitkit CA

    assert forge_api.main(["GET", "/repos/t/x"]) == 1
    assert seen[0].get_header("Authorization") == "token abc123"
    assert "abc123" not in seen[0].full_url
    assert "HTTP 404" in capsys.readouterr().err


def test_forge_api_without_arguments_prints_usage(forge_api, capsys):
    assert forge_api.main([]) == 2
    assert "METHOD PATH" in capsys.readouterr().err
