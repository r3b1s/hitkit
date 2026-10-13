"""Kit knobs (sizing and feature toggles) and kit-level hook wiring (design §9)."""

from __future__ import annotations

import pytest

from hitkit import kits
from hitkit.errors import HitkitError


def test_forge_defaults_to_one_runner_and_renovate():
    kit = kits.load("forge")
    assert kit.default_knobs() == {"runners": 1, "ephemeral": 0, "arch": False, "renovate": True}
    assert tuple(m.name for m in kit.machines) == ("forgejo", "runner-1")


def test_runners_knob_scales_runner_machines():
    kit = kits.load("forge")
    knobs = kit.parse_knobs(["--runners", "3"])
    assert knobs["runners"] == 3
    assert [m.name for m in kit.expand(knobs)] == ["forgejo", "runner-1", "runner-2", "runner-3"]


def test_equals_form_is_accepted():
    assert kits.load("forge").parse_knobs(["--runners=2"])["runners"] == 2


def test_no_renovate_and_renovate_toggle_the_flag():
    kit = kits.load("forge")
    assert kit.parse_knobs(["--no-renovate"])["renovate"] is False
    assert kit.parse_knobs(["--no-renovate", "--renovate"])["renovate"] is True


def test_bool_knob_honors_inline_value():
    forge = kits.load("forge")
    assert forge.parse_knobs(["--renovate=false"])["renovate"] is False
    assert forge.parse_knobs(["--renovate=yes"])["renovate"] is True
    with pytest.raises(HitkitError):
        forge.parse_knobs(["--renovate=maybe"])
    with pytest.raises(HitkitError):
        forge.parse_knobs(["--no-renovate=true"])


def test_negating_an_int_knob_is_an_error():
    with pytest.raises(HitkitError):
        kits.load("forge").parse_knobs(["--no-runners", "3"])


def test_unknown_knob_raises():
    with pytest.raises(HitkitError, match="no knob --bogus"):
        kits.load("forge").parse_knobs(["--bogus"])


@pytest.mark.parametrize("value", ["-1", "11"])
def test_runners_outside_bounds_raises(value):
    with pytest.raises(HitkitError, match="between 0 and 10"):
        kits.load("forge").parse_knobs(["--runners", value])


@pytest.mark.parametrize("args", [["--runners", "many"], ["--runners"]])
def test_non_integer_runners_raises(args):
    with pytest.raises(HitkitError, match="expects an integer"):
        kits.load("forge").parse_knobs(args)


def test_bare_positional_argument_raises():
    with pytest.raises(HitkitError, match="unexpected argument"):
        kits.load("forge").parse_knobs(["3"])


def test_devbox_has_no_knobs():
    assert kits.load("devbox").parse_knobs([]) == {}


def test_devbox_hooks_are_base_class_with_no_credentials():
    hooks = kits.hooks("devbox")
    machine = kits.load("devbox").machines[0]
    assert type(hooks) is kits.Hooks
    assert hooks.credentials(None, machine, ()) == {}


def test_forge_hooks_expose_token_create():
    assert callable(getattr(kits.hooks("forge"), "token_create", None))


def test_parse_rejects_count_knob_that_is_not_declared():
    text = """
name = "x"
description = "d"
version = 1

[[machine]]
name = "runner"
role = "runner"
count_knob = "runners"
cpus = 1
memory_mib = 1024
data_disk_gib = 1
"""
    with pytest.raises(ValueError, match="undeclared knob"):
        kits.parse(text)


def test_knobs_not_given_keep_their_previous_values():
    forge = kits.load("forge")
    previous = {"runners": 0, "ephemeral": 3, "renovate": False, "stale": 1}
    assert forge.parse_knobs([], previous) == {"runners": 0, "ephemeral": 3, "arch": False, "renovate": False}
    assert forge.parse_knobs(["--ephemeral", "0"], previous)["ephemeral"] == 0
    assert forge.parse_knobs(["--renovate"], previous)["renovate"] is True


def test_zero_persistent_runners_expands_to_forgejo_only():
    forge = kits.load("forge")
    assert [m.name for m in forge.expand(forge.parse_knobs(["--runners", "0"]))] == ["forgejo"]
