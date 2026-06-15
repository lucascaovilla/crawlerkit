"""The impersonate-target map is extensible from the outside, not just by editing identity.py."""

from crawlerkit.core.identity import (
    _IMPERSONATE_BY_MAJOR,
    ProfileGenerator,
    available_chrome_targets,
)


def test_default_targets_used_when_none_given() -> None:
    gen = ProfileGenerator()
    assert gen._targets == _IMPERSONATE_BY_MAJOR


def test_custom_targets_are_honored_over_the_default() -> None:
    custom = [(50, "chrome50"), (200, "chrome200")]
    gen = ProfileGenerator(impersonate_targets=custom)
    assert gen._targets == custom

    profile = gen.generate()
    # browserforge is installed in this env, so generate() actually runs the snap; whatever Chrome
    # major it picks, the chosen impersonate target must come from the custom list, not the default.
    assert profile.impersonate in {t for _, t in custom}


def test_available_chrome_targets_returns_parseable_chrome_majors() -> None:
    targets = available_chrome_targets()
    assert targets is None or all(
        isinstance(major, int) and name.startswith("chrome") for major, name in targets
    )
