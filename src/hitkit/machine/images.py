"""Locally built, cached machine images (design §8.1).

Each role is a complete mkosi configuration under ``hitkit/images/<role>``
(its own distribution); every role also pulls in the shared guest pieces from
``hitkit/images/common`` via ExtraTrees/RepartDirectories. The cache key is a hash of the configuration, so editing
a profile (or bumping a pin) produces a new image while existing VMs keep
booting from the old one until `gc` finds it unreferenced.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import tarfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hitkit import trace
from hitkit.errors import HitkitError
from hitkit.machine import disks
from hitkit.paths import UserContext

IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"
COMMON_DIR = "common"
# Packages whose versions are surfaced in metadata and `hitkit status` (Debian and Arch names).
TRACKED_PACKAGES = ("linux-image-amd64", "linux", "systemd", "openssh-server", "openssh", "libvirt", "python", "docker-ce", "git")


@dataclass(frozen=True)
class Image:
    role: str
    hash: str
    path: Path
    versions: dict[str, str]

    @property
    def id(self) -> str:
        return f"{self.role}-{self.hash}"


_INCLUDE = re.compile(r"^\s*Include=\.\./([\w-]+)\s*$", re.MULTILINE)
_EXTRA_TREE = re.compile(r"^\s*ExtraTrees=\.\./([\w-]+)/", re.MULTILINE)


def role_dirs(role: str) -> list[Path]:
    """Directories whose content defines the role's image: shared pieces, the
    roles it includes (``Include=../<role>``), then the role itself."""
    role_dir = IMAGES_DIR / role
    if role in (COMMON_DIR, "dev-common", "arch-common") or not (role_dir / "mkosi.conf").is_file():
        raise HitkitError(f"unknown image role {role!r}")
    dirs = [IMAGES_DIR / COMMON_DIR]
    conf = (role_dir / "mkosi.conf").read_text()
    for included in _INCLUDE.findall(conf):
        dirs += [d for d in role_dirs(included) if d not in dirs]
    for tree in _EXTRA_TREE.findall(conf):  # e.g. ../dev-common/mkosi.extra
        if IMAGES_DIR / tree not in dirs:
            dirs.append(IMAGES_DIR / tree)
    return dirs + [role_dir]


@dataclass(frozen=True)
class Artifact:
    """A pinned third-party file placed into the image (pins.toml, design §8.1)."""

    name: str
    version: str
    url: str
    sha256: str
    dest: str
    mode: int
    member: str | None = None  # url is a .tar.gz; extract this file (the archive is what sha256 pins)


def pinned_artifacts(dirs: list[Path]) -> list[Artifact]:
    arts = []
    for d in dirs:
        pins = d / "pins.toml"
        if pins.is_file():
            arts += [Artifact(**a) for a in tomllib.loads(pins.read_text()).get("artifact", [])]
    return arts


def fetch_artifact(user: UserContext, art: Artifact, log: Callable[[str], None]) -> Path:
    """Download (once) and verify a pinned artifact. Returns the cached path."""
    cache = user.cache_dir / "downloads" / art.sha256
    if cache.exists():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {art.name} {art.version}")
    tmp = cache.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(art.url, timeout=60) as resp, tmp.open("wb") as out:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise HitkitError(f"downloading {art.name} {art.version} failed: {e}") from None
    if digest.hexdigest() != art.sha256:
        tmp.unlink()
        raise HitkitError(
            f"checksum mismatch for {art.name} {art.version}: got {digest.hexdigest()}",
            hint=f"refusing to use {art.url}",
        )
    tmp.replace(cache)
    return cache


def stage_artifacts(user: UserContext, arts: list[Artifact], dest: Path, log: Callable[[str], None]) -> None:
    for art in arts:
        target = dest / art.dest.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        cached = fetch_artifact(user, art, log)
        if art.member is None:
            shutil.copyfile(cached, target)
        else:
            with tarfile.open(cached, "r:gz") as tar:
                src = tar.extractfile(art.member)
                if src is None:
                    raise HitkitError(f"{art.member} not found in {art.url}")
                target.write_bytes(src.read())
        target.chmod(art.mode)


def distribution(role: str) -> str:
    """The Distribution= a role builds (its own or an included role's)."""
    for d in reversed(role_dirs(role)):
        conf = d / "mkosi.conf"
        if conf.is_file():
            if m := re.search(r"^\s*Distribution=(\S+)", conf.read_text(), re.MULTILINE):
                return m.group(1)
    return "debian"


_PACKAGE = re.compile(r"^[a-z0-9@._+-]+$")


def user_config_path(user: UserContext, role: str) -> Path:
    """Per-user additions to a role (design §13.15): `packages = [...]`."""
    return user.config_dir / "images" / f"{role}.toml"


def extra_packages(user: UserContext | None, role: str) -> list[str]:
    """Packages (in the role distribution's names) the user adds to a role's image."""
    if user is None:
        return []
    path = user_config_path(user, role)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as e:
        raise HitkitError(f"{path}: {e}") from None
    packages = raw.get("packages", [])
    bad = [p for p in packages if not isinstance(p, str) or not _PACKAGE.match(p)]
    if not isinstance(packages, list) or bad:
        raise HitkitError(f"{path}: packages must be a list of package names", hint=f"not valid: {bad}")
    return sorted(set(packages))


def config_hash(dirs: list[Path], packages: list[str] | None = None) -> str:
    """Stable hash of every file (path, exec bit, content) in the role's configuration,
    plus the user's extra packages (only when there are any, so other ids never move)."""
    h = hashlib.sha256()
    for d in dirs:
        for f in sorted(p for p in d.rglob("*") if p.is_file() and "mkosi.tools" not in p.parts):
            h.update(f"{d.name}/{f.relative_to(d)}\0{f.stat().st_mode & 0o111:o}\0".encode())
            h.update(f.read_bytes())
            h.update(b"\0")
    if packages:
        h.update(b"extra-packages\0" + "\n".join(packages).encode())
    return h.hexdigest()[:12]


def images_dir(user: UserContext) -> Path:
    return user.cache_dir / "images"


def _summary_path(user: UserContext, image_id: str) -> Path:
    return images_dir(user) / f"{image_id}.json"


def _load(summary: Path) -> Image:
    data = json.loads(summary.read_text())
    return Image(data["role"], data["hash"], summary.with_suffix(".qcow2"), data["versions"])


def image_id(role: str, user: UserContext | None = None) -> str:
    """The id the role's *current* configuration (and the user's additions) builds to."""
    return f"{role}-{config_hash(role_dirs(role), extra_packages(user, role))}"


def find(user: UserContext, role: str) -> Image | None:
    """The cached image for the role's *current* configuration, if built."""
    image_id_ = image_id(role, user)
    summary = _summary_path(user, image_id_)
    if summary.exists() and summary.with_suffix(".qcow2").exists():
        return _load(summary)
    return None


def list_images(user: UserContext) -> list[Image]:
    d = images_dir(user)
    if not d.exists():
        return []
    return [_load(s) for s in sorted(d.glob("*.json")) if not s.name.endswith(".manifest.json")]


def versions_from_manifest(manifest: dict) -> dict[str, str]:
    cfg = manifest.get("config", {})
    versions = {"distribution": f"{cfg.get('distribution', '?')} {cfg.get('release', '?')}"}
    for pkg in manifest.get("packages", []):
        if pkg.get("name") in TRACKED_PACKAGES:
            versions[pkg["name"]] = pkg["version"]
    return versions


def build(user: UserContext, role: str, log: Callable[[str], None] = lambda _m: None) -> Image:
    import fcntl

    # Builds share one working tree (and mkosi's caches): one at a time, across processes.
    lock_path = user.cache_dir / "build" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock, trace.span("image.build", role=role) as result:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another image build is running; waiting for it to finish")
            fcntl.flock(lock, fcntl.LOCK_EX)
        image = _build(user, role, log)
        result.update(image=image.id, versions=image.versions)
        return image


def _build(user: UserContext, role: str, log: Callable[[str], None]) -> Image:
    dirs = role_dirs(role)
    packages = extra_packages(user, role)
    image_hash = config_hash(dirs, packages)
    image_id = f"{role}-{image_hash}"
    out = user.cache_dir / "build" / role
    _force_rmtree(out)
    out.mkdir(parents=True)
    arts = pinned_artifacts(dirs)
    staging = out / "artifacts"
    stage_artifacts(user, arts, staging, log)
    log(f"building image {image_id} with mkosi (first build downloads packages; this takes a few minutes)")
    from hitkit import hostinfo

    workdir = _sync_workdir(user)
    shared_tools = user.cache_dir / "mkosi-tools"
    tools_tree: list[str] = []
    if not hostinfo.can_build_natively(distribution(role)):
        log(f"this host lacks native {distribution(role)} build tools; using mkosi's tools tree")
        if shared_tools.is_dir():
            tools_tree = [f"--tools-tree={shared_tools}"]
        else:
            tools_tree = ["--tools-tree=yes"]
            if release := hostinfo.detect().tools_tree_release:
                tools_tree.append(f"--tools-tree-release={release}")
    mkosi = hostinfo.mkosi_binary()
    if problem := hostinfo.mkosi_problem(hostinfo.mkosi_version(mkosi)):
        raise HitkitError(problem, hint=hostinfo.MKOSI_UPGRADE_HINT)
    cmd = [
        mkosi,
        *tools_tree,
        "--directory", str(workdir / role),
        "--output-directory", str(out),
        *(["--extra-tree", str(staging)] if arts else []),
        "--cache-directory", str(user.cache_dir / "mkosi-cache"),
        "--image-id", "hitkit-image",
        *[f"--package={p}" for p in packages],
        "--force",
        "build",
    ]
    try:
        result = trace.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise HitkitError("mkosi not found", hint="pacman -S mkosi apt debian-archive-keyring systemd-ukify") from None
    if result.returncode != 0:
        log_file = user.cache_dir / "build" / f"{role}.mkosi.log"
        log_file.write_text(result.stdout + result.stderr)
        trace.logger(__name__).error("mkosi build failed", role=role, mkosi_log=log_file)
        raise HitkitError(f"mkosi build failed for role {role!r}", hint=f"see {log_file}")

    built_tools = workdir / role / "mkosi.tools"
    if built_tools.is_dir() and not shared_tools.exists():
        # Reused by every later build that needs it. Renaming a directory needs
        # write permission on it, and mkosi leaves the tree's root read-only.
        mode = built_tools.stat().st_mode
        built_tools.chmod(mode | 0o200)
        built_tools.replace(shared_tools)
        shared_tools.chmod(mode)
    dest_dir = images_dir(user)
    dest_dir.mkdir(parents=True, exist_ok=True)
    qcow2 = dest_dir / f"{image_id}.qcow2"
    tmp = qcow2.with_suffix(".qcow2.tmp")
    log(f"converting to {qcow2.name}")
    disks.convert_to_qcow2(out / "hitkit-image.raw", tmp)
    tmp.chmod(0o444)  # backing files must never be written
    tmp.replace(qcow2)

    manifest = json.loads((out / "hitkit-image.manifest").read_text())
    (dest_dir / f"{image_id}.manifest.json").write_text(json.dumps(manifest, indent=1))
    versions = versions_from_manifest(manifest) | {a.name: a.version for a in arts}
    image = Image(role, image_hash, qcow2, versions)
    summary = {
        "role": role,
        "hash": image_hash,
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "versions": image.versions,
    }
    _summary_path(user, image_id).write_text(json.dumps(summary, indent=2))
    shutil.rmtree(out, ignore_errors=True)
    return image


def _force_rmtree(path: Path) -> None:
    """rmtree that also handles read-only directories (mkosi leaves some behind)."""
    import os
    import stat

    def onexc(func, failed, _exc):
        parent = os.path.dirname(failed)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        if os.path.isdir(failed) and not os.path.islink(failed):
            os.chmod(failed, os.stat(failed).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        func(failed)

    if path.exists():
        shutil.rmtree(path, onexc=onexc)


def _sync_workdir(user: UserContext) -> Path:
    """Copy the images tree into the cache and build from there: mkosi may write
    into the configuration directory (e.g. a tools tree), and the package's own
    files must stay untouched (and may be read-only)."""
    workdir = user.cache_dir / "build" / "images"
    _force_rmtree(workdir)
    shutil.copytree(IMAGES_DIR, workdir, symlinks=True, ignore=shutil.ignore_patterns("mkosi.tools*", "__pycache__"))
    return workdir


def ensure(user: UserContext, role: str, log: Callable[[str], None] = lambda _m: None) -> Image:
    return find(user, role) or build(user, role, log)


def remove(image: Image) -> None:
    for suffix in (".qcow2", ".json", ".manifest.json"):
        image.path.with_name(f"{image.id}{suffix}").unlink(missing_ok=True)
