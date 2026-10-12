"""Unit tests: pronunciation injection (longest-first, case-insensitive, no partials)."""
from tellybear.pron.inject import inject_with_lexicon


def test_basic_wrap():
    out = inject_with_lexicon("Hello Leo", [("Leo", "ˈliːoʊ")])
    assert out == "Hello [Leo](/ˈliːoʊ/)"


def test_case_insensitive_preserves_original_casing():
    out = inject_with_lexicon("LEO and leo", [("Leo", "k1")])
    assert out == "[LEO](/k1/) and [leo](/k1/)"


def test_longest_first_no_clobber():
    # "Ariana" must be wrapped as a whole; "Ari" must not break it apart.
    out = inject_with_lexicon("Ariana played with Ari.", [("Ari", "aa"), ("Ariana", "ahriana")])
    assert "[Ariana](/ahriana/)" in out
    assert "[Ari](/aa/)" in out
    # the Ariana wrap must be intact (no nested/partial Ari inside it)
    assert "[Ariana](/ahriana/)" in out and "[[Ari]" not in out


def test_no_partial_word_hit():
    # "Ana" should not match inside "banana"
    out = inject_with_lexicon("I ate a banana.", [("Ana", "aa")])
    assert out == "I ate a banana."


def test_only_entries_with_ipa_are_wrapped():
    out = inject_with_lexicon("Leo and Mira", [("Leo", "k1"), ("Mira", "")])
    assert "[Leo](/k1/)" in out
    assert "Mira" in out and "[Mira]" not in out
