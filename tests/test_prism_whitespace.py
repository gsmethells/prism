"""Tests for Prism's configurable whitespace options."""

import pytest

import black
from tests.util import assert_format


def test_space_around_infix_operators() -> None:
    """Test that spaces are added around infix operators when enabled."""
    source = "a+b*c-d/e"
    expected = "a + b * c - d / e\n"
    mode = black.Mode(space_around_infix_operators=True)
    assert_format(source, expected, mode)


def test_space_around_kwargs_equals() -> None:
    """Test that spaces are added around kwargs equals signs when enabled."""
    source = "func(a=1, b=2)"
    expected = "func(a = 1, b = 2)\n"
    mode = black.Mode(space_around_kwargs_equals=True)
    assert_format(source, expected, mode)


def test_space_around_dict_colons() -> None:
    """Test that spaces are added around dict colons when enabled."""
    source = "{'a': 1, 'b': 2}"
    expected = "{'a' : 1, 'b' : 2}\n"
    mode = black.Mode(space_around_dict_colons=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_all_whitespace_options() -> None:
    """Test that all whitespace options work together."""
    source = "result = func(a=1) + {'b': 2}"
    expected = "result = func(a = 1) + {'b' : 2}\n"
    mode = black.Mode(
        space_around_infix_operators=True,
        space_around_kwargs_equals=True,
        space_around_dict_colons=True,
        string_normalization=False,
    )
    assert_format(source, expected, mode)


def test_whitespace_options_disabled() -> None:
    """Test that whitespace options are disabled by default."""
    source = "result = func(a=1) + {'b': 2}"
    expected = "result = func(a=1) + {'b': 2}\n"  # Preserves single quotes when disabled
    mode = black.Mode(string_normalization=False)
    assert_format(source, expected, mode)


def test_complex_expressions_with_whitespace() -> None:
    """Test complex expressions with whitespace options enabled."""
    source = """
def calculate(x=1, y=2):
    return x+y*2-3/z
"""
    expected = """def calculate(x = 1, y = 2):
    return x + y * 2 - 3 / z
"""
    mode = black.Mode(
        space_around_infix_operators=True,
        space_around_kwargs_equals=True,
        string_normalization=False,
    )
    assert_format(source, expected, mode)


def test_dict_comprehension_with_whitespace() -> None:
    """Test dict comprehensions with whitespace options enabled."""
    source = "{k: v for k, v in items if k in valid_keys}"
    expected = "{k : v for k, v in items if k in valid_keys}\n"
    mode = black.Mode(space_around_dict_colons=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_kwargs_in_function_call_multiline() -> None:
    source = """func(
    a=1,
    b=2,
    c=3
)"""
    expected = "func(a = 1, b = 2, c = 3)\n"  # Black collapses to single line
    mode = black.Mode(space_around_kwargs_equals=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_kwargs_in_function_def_multiline() -> None:
    source = """def foo(
    a=1,
    b=2,
    c=3
):
    pass"""
    expected = "def foo(a = 1, b = 2, c = 3):\n    pass\n"  # Black collapses to single line
    mode = black.Mode(space_around_kwargs_equals=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_dict_literal_multiline() -> None:
    source = """{
    'a': 1,
    'b': 2,
    'c': 3
}"""
    expected = "{'a' : 1, 'b' : 2, 'c' : 3}\n"  # Black collapses to single line
    mode = black.Mode(space_around_dict_colons=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_dict_comprehension_multiline() -> None:
    source = """{k: v for k, v in items\nif k in valid_keys}"""
    expected = "{k : v for k, v in items if k in valid_keys}\n"  # Black collapses to single line
    mode = black.Mode(space_around_dict_colons=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_nested_dict_and_kwargs() -> None:
    source = """foo(a=1, b={'x':2, 'y':3})"""
    expected = "foo(a = 1, b = {'x' : 2, 'y' : 3})\n"
    mode = black.Mode(space_around_kwargs_equals=True, space_around_dict_colons=True, string_normalization=False)
    assert_format(source, expected, mode)


def test_edge_case_no_space_when_disabled() -> None:
    source = "foo(a=1, b={'x':2, 'y':3})"
    expected = "foo(a=1, b={'x': 2, 'y': 3})\n"  # Preserves single quotes when disabled
    mode = black.Mode(string_normalization=False)
    assert_format(source, expected, mode) 