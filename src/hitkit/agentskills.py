"""Agent skills shipped with hitkit (the open Agent Skills format: <name>/SKILL.md).

`hitkit skills` lists them, `hitkit skills print NAME` shows one, and
`hitkit skills install [DIR]` copies them into the skills directory of whichever
agent harness the user runs.

The skills live in `skills/` at the repository root, where `npx skills` finds them.
The wheel bundles that directory as `hitkit/skills` at build time (pyproject.toml),
so a source checkout, including an editable install, reads the repository's own copy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from hitkit.errors import Conflict, HitkitError



def _skills_dir() -> Path:
    package = Path(__file__).resolve().parent
    for candidate in (package / "skills", package.parents[1] / "skills"):  # wheel, then checkout
        if candidate.is_dir():
            return candidate
    return package / "skills"


SKILLS_DIR = _skills_dir()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


def frontmatter(text: str) -> dict[str, str]:
    """The flat `key: value` lines of a SKILL.md header (all hitkit's skills use)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    return fields


def available(root: Path = SKILLS_DIR) -> list[Skill]:
    skills = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        meta = frontmatter(skill_md.read_text())
        skills.append(Skill(meta.get("name", ""), meta.get("description", ""), skill_md.parent))
    return skills


def find(name: str, root: Path = SKILLS_DIR) -> Skill:
    skills = available(root)
    for skill in skills:
        if skill.name == name:
            return skill
    raise HitkitError(f"no bundled skill named {name!r}",
                      hint=f"available: {', '.join(s.name for s in skills) or 'none'}")


def install(dest: Path, root: Path = SKILLS_DIR) -> list[Path]:
    """Copy every skill to dest/<name>, replacing earlier copies of the same skill only."""
    skills = available(root)
    for skill in skills:
        target = dest / skill.name
        if target.exists():
            existing = target / "SKILL.md"
            if not existing.is_file() or frontmatter(existing.read_text()).get("name") != skill.name:
                raise Conflict(f"{target} exists and is not a copy of hitkit's {skill.name!r} skill")
    installed = []
    for skill in skills:
        target = dest / skill.name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(skill.path, target)
        installed.append(target)
    return installed
