"""Pure name-comparison helper behind the verified locator fallback ladder.
No browser, no surface, no engine -- see tests/e2e/test_fallback_targets.py and
tests/unit/test_terminal.py for the same ladder wired into a real surface.
`names_equal` (the `normalized` rung) is the ONLY rung the ladder may ever
act on -- see docs/DESIGN_CHOICES.md for why a broader match must stop and
ask for review instead.
"""

from computer_use_replay.fallback import names_equal, normalize


def test_normalize_casefolds_and_collapses_whitespace():
    assert normalize("  Find   Member  ") == "find member"
    assert normalize("FIND MEMBER") == "find member"


def test_normalize_strips_decorative_punctuation_only():
    assert normalize("Find member…") == "find member"
    assert normalize("Find member...") == "find member"
    assert normalize("Find member:") == "find member"
    assert normalize("*Find member*") == "find member"
    assert normalize("»Find member›") == "find member"
    assert normalize("(Find member)") == "find member"
    # Real wording punctuation (a hyphen inside a word) is not decoration and stays.
    assert normalize("Sub-account nickname") == "sub-account nickname"


def test_normalize_applies_unicode_nfkc():
    # A fullwidth colon and fullwidth Latin letters normalize (NFKC) before the
    # decoration strip and casefold run, so a fullwidth relabel still matches.
    assert normalize("Ｆｉｎｄｍｅｍｂｅｒ：") == (normalize("Findmember:"))


def test_normalize_never_drops_a_letter_digit_or_word():
    # These must all still differ from "find member" after normalization --
    # only case, Unicode form, whitespace and pure decoration are insensitive.
    # This is the exact defect an external review found: a broader "similar"
    # match used to rescue these onto the wrong control.
    reviewed = normalize("Find member")
    for text in (
        "Find member 00123",
        "Find a member",
        "Do not Find member",
        "Find member and delete",
        "Find members",
    ):
        assert normalize(text) != reviewed, text


def test_names_equal_ignores_decoration_case_and_unicode_form():
    assert names_equal("Find member…", "Find Member") is True
    assert names_equal("FIND MEMBER:", "find member") is True
    assert names_equal("»FIND MEMBER›", "Find member") is True
    assert names_equal("(Find member)", "Find member") is True
    assert names_equal("Ｆｉｎｄ ｍｅｍｂｅｒ：", "Find member") is True


def test_names_equal_requires_the_same_words():
    assert names_equal("Find a member", "Find member") is False
    assert names_equal("Find member 00123", "Find member") is False
    assert names_equal("Do not Find member", "Find member") is False
    assert names_equal("Find member and delete", "Find member") is False
    assert names_equal("Find members", "Find member") is False


def test_names_equal_rejects_empty_names():
    assert names_equal("", "Find member") is False
    assert names_equal("Find member", "") is False
    assert names_equal("", "") is False
