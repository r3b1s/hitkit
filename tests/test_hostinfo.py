from hitkit import hostinfo


def test_parse_os_release_families():
    assert hostinfo.parse_os_release('ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS"\n').family == "arch"
    assert hostinfo.parse_os_release('ID=debian\nPRETTY_NAME="Debian GNU/Linux 13"\n').family == "debian"
    assert hostinfo.parse_os_release('ID=ubuntu\nID_LIKE=debian\n').family == "debian"
    assert hostinfo.parse_os_release('ID=fedora\n').family == "fedora"
    assert hostinfo.parse_os_release('ID=rocky\nID_LIKE="rhel centos fedora"\n').family == "fedora"
    assert hostinfo.parse_os_release('ID=gentoo\n').family == "unknown"


def test_host_package_hints_name_the_right_tool():
    assert hostinfo.Distro("debian", "").host_packages.startswith("apt ")
    assert hostinfo.Distro("fedora", "").host_packages.startswith("dnf ")
    assert hostinfo.Distro("arch", "").host_packages.startswith("pacman ")


def test_native_build_detection_searches_sbin(monkeypatch):
    found = []
    monkeypatch.setattr(hostinfo.shutil, "which", lambda b, path=None: found.append(path) or f"/usr/sbin/{b}")
    monkeypatch.setattr(hostinfo.Path, "exists", lambda self: True)
    assert hostinfo.can_build_natively("debian")
    assert all("/usr/sbin" in p for p in found)


def test_tools_tree_release_matches_host():
    fedora = hostinfo.parse_os_release('ID=fedora\nVERSION_ID=44\n')
    debian = hostinfo.parse_os_release('ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n')
    arch = hostinfo.parse_os_release('ID=arch\n')
    assert (fedora.tools_tree_release, debian.tools_tree_release, arch.tools_tree_release) == ("44", "trixie", None)


def test_mkosi_version_policy():
    assert hostinfo.mkosi_problem((27, 1)) is None
    assert hostinfo.mkosi_problem((25, 3)) is None
    assert "depmod" in hostinfo.mkosi_problem((26,))
    assert "too old" in hostinfo.mkosi_problem((24, 0))
    assert hostinfo.mkosi_problem(None) == "mkosi not found"


def test_arch_images_build_natively_only_on_arch_itself(monkeypatch):
    # Derivatives ship newer pacman builds whose local database Arch's pacman cannot fully read.
    from hitkit import hostinfo

    monkeypatch.setattr(hostinfo, "_have", lambda binary: True)
    monkeypatch.setattr(hostinfo.Path, "exists", lambda self: True)
    cachyos = hostinfo.parse_os_release('ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS"\n')
    arch = hostinfo.parse_os_release('ID=arch\nPRETTY_NAME="Arch Linux"\n')

    assert cachyos.family == "arch" and cachyos.id == "cachyos"
    assert hostinfo.can_build_natively("arch", cachyos) is False
    assert hostinfo.can_build_natively("arch", arch) is True
