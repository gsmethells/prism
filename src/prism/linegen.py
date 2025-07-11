"""
Generating lines of code.
"""
import sys
from enum import Enum, auto
from functools import partial, wraps
from typing import Collection, Iterator, List, Optional, Set, Union, cast

from prism.brackets import (
    COMMA_PRIORITY,
    DOT_PRIORITY,
    get_leaves_inside_matching_brackets,
    max_delimiter_priority_in_atom,
)
from prism.comments import FMT_OFF, generate_comments, list_comments
from prism.lines import (
    Line,
    append_leaves,
    can_be_split,
    can_omit_invisible_parens,
    is_line_short_enough,
    line_to_string,
)
from prism.mode import Feature, Mode, Preview
from prism.nodes import (
    ASSIGNMENTS,
    CLOSING_BRACKETS,
    OPENING_BRACKETS,
    RARROW,
    STANDALONE_COMMENT,
    STATEMENT,
    WHITESPACE,
    Visitor,
    ensure_visible,
    is_arith_like,
    is_atom_with_invisible_parens,
    is_docstring,
    is_empty_tuple,
    is_lpar_token,
    is_multiline_string,
    is_name_token,
    is_one_sequence_between,
    is_one_tuple,
    is_rpar_token,
    is_stub_body,
    is_stub_suite,
    is_vararg,
    is_walrus_assignment,
    is_yield,
    syms,
    wrap_in_parentheses,
)
from prism.numerics import normalize_numeric_literal
from prism.strings import (
    fix_docstring,
    get_string_prefix,
    normalize_string_prefix,
    normalize_string_quotes,
)
from prism.trans import (
    CannotTransform,
    StringMerger,
    StringParenStripper,
    StringParenWrapper,
    StringSplitter,
    Transformer,
    hug_power_op,
)
from blib2to3.pgen2 import token
from blib2to3.pytree import Leaf, Node

# types
LeafID = int
LN = Union[Leaf, Node]


class CannotSplit(CannotTransform):
    """A readable split that fits the allotted line length is impossible."""


# This isn't a dataclass because @dataclass + Generic breaks mypyc.
# See also https://github.com/mypyc/mypyc/issues/827.
class LineGenerator(Visitor[Line]):
    """Generates reformatted Line objects.  Empty lines are not emitted.

    Note: destroys the tree it's visiting by mutating prefixes of its leaves
    in ways that will no longer stringify to valid Python code on the tree.
    """

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.current_line: Line
        self.__post_init__()

    def line(self, indent: int = 0) -> Iterator[Line]:
        """Generate a line.

        If the line is empty, only emit if it makes sense.
        If the line is too long, split it first and then generate.

        If any lines were generated, set up a new current_line.
        """
        if not self.current_line:
            self.current_line.depth += indent
            return  # Line is empty, don't emit. Creating a new one unnecessary.

        complete_line = self.current_line
        self.current_line = Line(mode=self.mode, depth=complete_line.depth + indent)
        yield complete_line

    def visit_default(self, node: LN) -> Iterator[Line]:
        """Default `visit_*()` implementation. Recurses to children of `node`."""
        if isinstance(node, Leaf):
            any_open_brackets = self.current_line.bracket_tracker.any_open_brackets()
            for comment in generate_comments(node, preview=self.mode.preview):
                if any_open_brackets:
                    # any comment within brackets is subject to splitting
                    self.current_line.append(comment)
                elif comment.type == token.COMMENT:
                    # regular trailing comment
                    self.current_line.append(comment)
                    yield from self.line()

                else:
                    # regular standalone comment
                    yield from self.line()

                    self.current_line.append(comment)
                    yield from self.line()

            normalize_prefix(node, inside_brackets=any_open_brackets)
            if self.mode.string_normalization and node.type == token.STRING:
                node.value = normalize_string_prefix(node.value)
                node.value = normalize_string_quotes(node.value)
            if node.type == token.NUMBER:
                normalize_numeric_literal(node)
            if node.type not in WHITESPACE:
                self.current_line.append(node)
        yield from super().visit_default(node)

    def visit_INDENT(self, node: Leaf) -> Iterator[Line]:
        """Increase indentation level, maybe yield a line."""
        # In blib2to3 INDENT never holds comments.
        yield from self.line(+1)
        yield from self.visit_default(node)

    def visit_DEDENT(self, node: Leaf) -> Iterator[Line]:
        """Decrease indentation level, maybe yield a line."""
        # The current line might still wait for trailing comments.  At DEDENT time
        # there won't be any (they would be prefixes on the preceding NEWLINE).
        # Emit the line then.
        yield from self.line()

        # While DEDENT has no value, its prefix may contain standalone comments
        # that belong to the current indentation level.  Get 'em.
        yield from self.visit_default(node)

        # Finally, emit the dedent.
        yield from self.line(-1)

    def visit_stmt(
        self, node: Node, keywords: Set[str], parens: Set[str]
    ) -> Iterator[Line]:
        """Visit a statement.

        This implementation is shared for `if`, `while`, `for`, `try`, `except`,
        `def`, `with`, `class`, `assert`, and assignments.

        The relevant Python language `keywords` for a given statement will be
        NAME leaves within it. This methods puts those on a separate line.

        `parens` holds a set of string leaf values immediately after which
        invisible parens should be put.
        """
        normalize_invisible_parens(node, parens_after=parens, preview=self.mode.preview)
        for child in node.children:
            if is_name_token(child) and child.value in keywords:
                yield from self.line()

            yield from self.visit(child)

    def visit_funcdef(self, node: Node) -> Iterator[Line]:
        """Visit function definition."""
        if Preview.annotation_parens not in self.mode:
            yield from self.visit_stmt(node, keywords={"def"}, parens=set())
        else:
            yield from self.line()

            # Remove redundant brackets around return type annotation.
            is_return_annotation = False
            for child in node.children:
                if child.type == token.RARROW:
                    is_return_annotation = True
                elif is_return_annotation:
                    if child.type == syms.atom and child.children[0].type == token.LPAR:
                        if maybe_make_parens_invisible_in_atom(
                            child,
                            parent=node,
                            remove_brackets_around_comma=False,
                        ):
                            wrap_in_parentheses(node, child, visible=False)
                    else:
                        wrap_in_parentheses(node, child, visible=False)
                    is_return_annotation = False

            for child in node.children:
                yield from self.visit(child)

    def visit_match_case(self, node: Node) -> Iterator[Line]:
        """Visit either a match or case statement."""
        normalize_invisible_parens(node, parens_after=set(), preview=self.mode.preview)

        yield from self.line()
        for child in node.children:
            yield from self.visit(child)

    def visit_suite(self, node: Node) -> Iterator[Line]:
        """Visit a suite."""
        if self.mode.is_pyi and is_stub_suite(node):
            yield from self.visit(node.children[2])
        else:
            yield from self.visit_default(node)

    def visit_simple_stmt(self, node: Node) -> Iterator[Line]:
        """Visit a statement without nested statements."""
        prev_type: Optional[int] = None
        for child in node.children:
            if (prev_type is None or prev_type == token.SEMI) and is_arith_like(child):
                wrap_in_parentheses(node, child, visible=False)
            prev_type = child.type

        is_suite_like = node.parent and node.parent.type in STATEMENT
        if is_suite_like:
            if self.mode.is_pyi and is_stub_body(node):
                yield from self.visit_default(node)
            else:
                yield from self.line(+1)
                yield from self.visit_default(node)
                yield from self.line(-1)

        else:
            if (
                not self.mode.is_pyi
                or not node.parent
                or not is_stub_suite(node.parent)
            ):
                yield from self.line()
            yield from self.visit_default(node)

    def visit_async_stmt(self, node: Node) -> Iterator[Line]:
        """Visit `async def`, `async for`, `async with`."""
        yield from self.line()

        children = iter(node.children)
        for child in children:
            yield from self.visit(child)

            if child.type == token.ASYNC or child.type == STANDALONE_COMMENT:
                # STANDALONE_COMMENT happens when `# fmt: skip` is applied on the async
                # line.
                break

        internal_stmt = next(children)
        for child in internal_stmt.children:
            yield from self.visit(child)

    def visit_decorators(self, node: Node) -> Iterator[Line]:
        """Visit decorators."""
        for child in node.children:
            yield from self.line()
            yield from self.visit(child)

    def visit_power(self, node: Node) -> Iterator[Line]:
        for idx, leaf in enumerate(node.children[:-1]):
            next_leaf = node.children[idx + 1]

            if not isinstance(leaf, Leaf):
                continue

            value = leaf.value.lower()
            if (
                leaf.type == token.NUMBER
                and next_leaf.type == syms.trailer
                # Ensure that we are in an attribute trailer
                and next_leaf.children[0].type == token.DOT
                # It shouldn't wrap hexadecimal, binary and octal literals
                and not value.startswith(("0x", "0b", "0o"))
                # It shouldn't wrap complex literals
                and "j" not in value
            ):
                wrap_in_parentheses(node, leaf)

        if Preview.remove_redundant_parens in self.mode:
            remove_await_parens(node)

        yield from self.visit_default(node)

    def visit_SEMI(self, leaf: Leaf) -> Iterator[Line]:
        """Remove a semicolon and put the other statement on a separate line."""
        yield from self.line()

    def visit_ENDMARKER(self, leaf: Leaf) -> Iterator[Line]:
        """End of file. Process outstanding comments and end with a newline."""
        yield from self.visit_default(leaf)
        yield from self.line()

    def visit_STANDALONE_COMMENT(self, leaf: Leaf) -> Iterator[Line]:
        if not self.current_line.bracket_tracker.any_open_brackets():
            yield from self.line()
        yield from self.visit_default(leaf)

    def visit_factor(self, node: Node) -> Iterator[Line]:
        """Force parentheses between a unary op and a binary power:

        -2 ** 8 -> -(2 ** 8)
        """
        _operator, operand = node.children
        if (
            operand.type == syms.power
            and len(operand.children) == 3
            and operand.children[1].type == token.DOUBLESTAR
        ):
            lpar = Leaf(token.LPAR, "(")
            rpar = Leaf(token.RPAR, ")")
            index = operand.remove() or 0
            node.insert_child(index, Node(syms.atom, [lpar, operand, rpar]))
        yield from self.visit_default(node)

    def visit_STRING(self, leaf: Leaf) -> Iterator[Line]:
        if is_docstring(leaf) and "\\\n" not in leaf.value:
            # We're ignoring docstrings with backslash newline escapes because changing
            # indentation of those changes the AST representation of the code.
            if Preview.normalize_docstring_quotes_and_prefixes_properly in self.mode:
                # There was a bug where --skip-string-normalization wouldn't stop us
                # from normalizing docstring prefixes. To maintain stability, we can
                # only address this buggy behaviour while the preview style is enabled.
                if self.mode.string_normalization:
                    docstring = normalize_string_prefix(leaf.value)
                    # visit_default() does handle string normalization for us, but
                    # since this method acts differently depending on quote style (ex.
                    # see padding logic below), there's a possibility for unstable
                    # formatting as visit_default() is called *after*. To avoid a
                    # situation where this function formats a docstring differently on
                    # the second pass, normalize it early.
                    docstring = normalize_string_quotes(docstring)
                else:
                    docstring = leaf.value
            else:
                # ... otherwise, we'll keep the buggy behaviour >.<
                docstring = normalize_string_prefix(leaf.value)
            prefix = get_string_prefix(docstring)
            docstring = docstring[len(prefix) :]  # Remove the prefix
            quote_char = docstring[0]
            # A natural way to remove the outer quotes is to do:
            #   docstring = docstring.strip(quote_char)
            # but that breaks on """""x""" (which is '""x').
            # So we actually need to remove the first character and the next two
            # characters but only if they are the same as the first.
            quote_len = 1 if docstring[1] != quote_char else 3
            docstring = docstring[quote_len:-quote_len]
            docstring_started_empty = not docstring
            indent = " " * 4 * self.current_line.depth

            if is_multiline_string(leaf):
                docstring = fix_docstring(docstring, indent)
            else:
                docstring = docstring.strip()

            if docstring:
                # Add some padding if the docstring starts / ends with a quote mark.
                if docstring[0] == quote_char:
                    docstring = " " + docstring
                if docstring[-1] == quote_char:
                    docstring += " "
                if docstring[-1] == "\\":
                    backslash_count = len(docstring) - len(docstring.rstrip("\\"))
                    if backslash_count % 2:
                        # Odd number of tailing backslashes, add some padding to
                        # avoid escaping the closing string quote.
                        docstring += " "
            elif not docstring_started_empty:
                docstring = " "

            # We could enforce triple quotes at this point.
            quote = quote_char * quote_len

            # It's invalid to put closing single-character quotes on a new line.
            if Preview.long_docstring_quotes_on_newline in self.mode and quote_len == 3:
                # We need to find the length of the last line of the docstring
                # to find if we can add the closing quotes to the line without
                # exceeding the maximum line length.
                # If docstring is one line, then we need to add the length
                # of the indent, prefix, and starting quotes. Ending quotes are
                # handled later.
                lines = docstring.splitlines()
                last_line_length = len(lines[-1]) if docstring else 0

                if len(lines) == 1:
                    last_line_length += len(indent) + len(prefix) + quote_len

                # If adding closing quotes would cause the last line to exceed
                # the maximum line length then put a line break before the
                # closing quotes
                if last_line_length + quote_len > self.mode.line_length:
                    leaf.value = prefix + quote + docstring + "\n" + indent + quote
                else:
                    leaf.value = prefix + quote + docstring + quote
            else:
                leaf.value = prefix + quote + docstring + quote

        yield from self.visit_default(leaf)

    def __post_init__(self) -> None:
        """You are in a twisty little maze of passages."""
        self.current_line = Line(mode=self.mode)

        v = self.visit_stmt
        Ø: Set[str] = set()
        self.visit_assert_stmt = partial(v, keywords={"assert"}, parens={"assert", ","})
        self.visit_if_stmt = partial(
            v, keywords={"if", "else", "elif"}, parens={"if", "elif"}
        )
        self.visit_while_stmt = partial(v, keywords={"while", "else"}, parens={"while"})
        self.visit_for_stmt = partial(v, keywords={"for", "else"}, parens={"for", "in"})
        self.visit_try_stmt = partial(
            v, keywords={"try", "except", "else", "finally"}, parens=Ø
        )
        if self.mode.preview:
            self.visit_except_clause = partial(
                v, keywords={"except"}, parens={"except"}
            )
            self.visit_with_stmt = partial(v, keywords={"with"}, parens={"with"})
        else:
            self.visit_except_clause = partial(v, keywords={"except"}, parens=Ø)
            self.visit_with_stmt = partial(v, keywords={"with"}, parens=Ø)
        self.visit_classdef = partial(v, keywords={"class"}, parens=Ø)
        self.visit_expr_stmt = partial(v, keywords=Ø, parens=ASSIGNMENTS)
        self.visit_return_stmt = partial(v, keywords={"return"}, parens={"return"})
        self.visit_import_from = partial(v, keywords=Ø, parens={"import"})
        self.visit_del_stmt = partial(v, keywords=Ø, parens={"del"})
        self.visit_async_funcdef = self.visit_async_stmt
        self.visit_decorated = self.visit_decorators

        # PEP 634
        self.visit_match_stmt = self.visit_match_case
        self.visit_case_block = self.visit_match_case


def transform_line(
    line: Line, mode: Mode, features: Collection[Feature] = ()
) -> Iterator[Line]:
    """Transform a `line`, potentially splitting it into many lines.

    They should fit in the allotted `line_length` but might not be able to.

    `features` are syntactical features that may be used in the output.
    """
    if line.is_comment:
        yield line
        return

    line_str = line_to_string(line)

    ll = mode.line_length
    sn = mode.string_normalization
    string_merge = StringMerger(ll, sn)
    string_paren_strip = StringParenStripper(ll, sn)
    string_split = StringSplitter(ll, sn)
    string_paren_wrap = StringParenWrapper(ll, sn)

    transformers: List[Transformer]
    if (
        not line.contains_uncollapsable_type_comments()
        and not line.should_split_rhs
        and not line.magic_trailing_comma
        and (
            is_line_short_enough(line, line_length=mode.line_length, line_str=line_str)
            or line.contains_unsplittable_type_ignore()
        )
        and not (line.inside_brackets and line.contains_standalone_comments())
    ):
        # Only apply basic string preprocessing, since lines shouldn't be split here.
        if Preview.string_processing in mode:
            transformers = [string_merge, string_paren_strip]
        else:
            transformers = []
    elif line.is_def:
        transformers = [left_hand_split]
    else:

        def _rhs(
            self: object, line: Line, features: Collection[Feature]
        ) -> Iterator[Line]:
            """Wraps calls to `right_hand_split`.

            The calls increasingly `omit` right-hand trailers (bracket pairs with
            content), meaning the trailers get glued together to split on another
            bracket pair instead.
            """
            for omit in generate_trailers_to_omit(line, mode.line_length):
                lines = list(
                    right_hand_split(line, mode.line_length, features, omit=omit)
                )
                # Note: this check is only able to figure out if the first line of the
                # *current* transformation fits in the line length.  This is true only
                # for simple cases.  All others require running more transforms via
                # `transform_line()`.  This check doesn't know if those would succeed.
                if is_line_short_enough(lines[0], line_length=mode.line_length):
                    yield from lines
                    return

            # All splits failed, best effort split with no omits.
            # This mostly happens to multiline strings that are by definition
            # reported as not fitting a single line, as well as lines that contain
            # trailing commas (those have to be exploded).
            yield from right_hand_split(
                line, line_length=mode.line_length, features=features
            )

        # HACK: nested functions (like _rhs) compiled by mypyc don't retain their
        # __name__ attribute which is needed in `run_transformer` further down.
        # Unfortunately a nested class breaks mypyc too. So a class must be created
        # via type ... https://github.com/mypyc/mypyc/issues/884
        rhs = type("rhs", (), {"__call__": _rhs})()

        if Preview.string_processing in mode:
            if line.inside_brackets:
                transformers = [
                    string_merge,
                    string_paren_strip,
                    string_split,
                    delimiter_split,
                    standalone_comment_split,
                    string_paren_wrap,
                    rhs,
                ]
            else:
                transformers = [
                    string_merge,
                    string_paren_strip,
                    string_split,
                    string_paren_wrap,
                    rhs,
                ]
        else:
            if line.inside_brackets:
                transformers = [delimiter_split, standalone_comment_split, rhs]
            else:
                transformers = [rhs]
    # It's always safe to attempt hugging of power operations and pretty much every line
    # could match.
    transformers.append(hug_power_op)

    for transform in transformers:
        # We are accumulating lines in `result` because we might want to abort
        # mission and return the original line in the end, or attempt a different
        # split altogether.
        try:
            result = run_transformer(line, transform, mode, features, line_str=line_str)
        except CannotTransform:
            continue
        else:
            yield from result
            break

    else:
        yield line


class _BracketSplitComponent(Enum):
    head = auto()
    body = auto()
    tail = auto()


def left_hand_split(line: Line, _features: Collection[Feature] = ()) -> Iterator[Line]:
    """Split line into many lines, starting with the first matching bracket pair.

    Note: this usually looks weird, only use this for function definitions.
    Prefer RHS otherwise.  This is why this function is not symmetrical with
    :func:`right_hand_split` which also handles optional parentheses.
    """
    tail_leaves: List[Leaf] = []
    body_leaves: List[Leaf] = []
    head_leaves: List[Leaf] = []
    current_leaves = head_leaves
    matching_bracket: Optional[Leaf] = None
    for leaf in line.leaves:
        if (
            current_leaves is body_leaves
            and leaf.type in CLOSING_BRACKETS
            and leaf.opening_bracket is matching_bracket
            and isinstance(matching_bracket, Leaf)
        ):
            ensure_visible(leaf)
            ensure_visible(matching_bracket)
            current_leaves = tail_leaves if body_leaves else head_leaves
        current_leaves.append(leaf)
        if current_leaves is head_leaves:
            if leaf.type in OPENING_BRACKETS:
                matching_bracket = leaf
                current_leaves = body_leaves
    if not matching_bracket:
        raise CannotSplit("No brackets found")

    head = bracket_split_build_line(
        head_leaves, line, matching_bracket, component=_BracketSplitComponent.head
    )
    body = bracket_split_build_line(
        body_leaves, line, matching_bracket, component=_BracketSplitComponent.body
    )
    tail = bracket_split_build_line(
        tail_leaves, line, matching_bracket, component=_BracketSplitComponent.tail
    )
    bracket_split_succeeded_or_raise(head, body, tail)
    for result in (head, body, tail):
        if result:
            yield result


def right_hand_split(
    line: Line,
    line_length: int,
    features: Collection[Feature] = (),
    omit: Collection[LeafID] = (),
) -> Iterator[Line]:
    """Split line into many lines, starting with the last matching bracket pair.

    If the split was by optional parentheses, attempt splitting without them, too.
    `omit` is a collection of closing bracket IDs that shouldn't be considered for
    this split.

    Note: running this function modifies `bracket_depth` on the leaves of `line`.
    """
    tail_leaves: List[Leaf] = []
    body_leaves: List[Leaf] = []
    head_leaves: List[Leaf] = []
    current_leaves = tail_leaves
    opening_bracket: Optional[Leaf] = None
    closing_bracket: Optional[Leaf] = None
    for leaf in reversed(line.leaves):
        if current_leaves is body_leaves:
            if leaf is opening_bracket:
                current_leaves = head_leaves if body_leaves else tail_leaves
        current_leaves.append(leaf)
        if current_leaves is tail_leaves:
            if leaf.type in CLOSING_BRACKETS and id(leaf) not in omit:
                opening_bracket = leaf.opening_bracket
                closing_bracket = leaf
                current_leaves = body_leaves
    if not (opening_bracket and closing_bracket and head_leaves):
        # If there is no opening or closing_bracket that means the split failed and
        # all content is in the tail.  Otherwise, if `head_leaves` are empty, it means
        # the matching `opening_bracket` wasn't available on `line` anymore.
        raise CannotSplit("No brackets found")

    tail_leaves.reverse()
    body_leaves.reverse()
    head_leaves.reverse()
    head = bracket_split_build_line(
        head_leaves, line, opening_bracket, component=_BracketSplitComponent.head
    )
    body = bracket_split_build_line(
        body_leaves, line, opening_bracket, component=_BracketSplitComponent.body
    )
    tail = bracket_split_build_line(
        tail_leaves, line, opening_bracket, component=_BracketSplitComponent.tail
    )
    bracket_split_succeeded_or_raise(head, body, tail)
    if (
        Feature.FORCE_OPTIONAL_PARENTHESES not in features
        # the opening bracket is an optional paren
        and opening_bracket.type == token.LPAR
        and not opening_bracket.value
        # the closing bracket is an optional paren
        and closing_bracket.type == token.RPAR
        and not closing_bracket.value
        # it's not an import (optional parens are the only thing we can split on
        # in this case; attempting a split without them is a waste of time)
        and not line.is_import
        # there are no standalone comments in the body
        and not body.contains_standalone_comments(0)
        # and we can actually remove the parens
        and can_omit_invisible_parens(body, line_length)
    ):
        omit = {id(closing_bracket), *omit}
        try:
            yield from right_hand_split(line, line_length, features=features, omit=omit)
            return

        except CannotSplit as e:
            if not (
                can_be_split(body)
                or is_line_short_enough(body, line_length=line_length)
            ):
                raise CannotSplit(
                    "Splitting failed, body is still too long and can't be split."
                ) from e

            elif head.contains_multiline_strings() or tail.contains_multiline_strings():
                raise CannotSplit(
                    "The current optional pair of parentheses is bound to fail to"
                    " satisfy the splitting algorithm because the head or the tail"
                    " contains multiline strings which by definition never fit one"
                    " line."
                ) from e

    ensure_visible(opening_bracket)
    ensure_visible(closing_bracket)
    for result in (head, body, tail):
        if result:
            yield result


def bracket_split_succeeded_or_raise(head: Line, body: Line, tail: Line) -> None:
    """Raise :exc:`CannotSplit` if the last left- or right-hand split failed.

    Do nothing otherwise.

    A left- or right-hand split is based on a pair of brackets. Content before
    (and including) the opening bracket is left on one line, content inside the
    brackets is put on a separate line, and finally content starting with and
    following the closing bracket is put on a separate line.

    Those are called `head`, `body`, and `tail`, respectively. If the split
    produced the same line (all content in `head`) or ended up with an empty `body`
    and the `tail` is just the closing bracket, then it's considered failed.
    """
    tail_len = len(str(tail).strip())
    if not body:
        if tail_len == 0:
            raise CannotSplit("Splitting brackets produced the same line")

        elif tail_len < 3:
            raise CannotSplit(
                f"Splitting brackets on an empty body to save {tail_len} characters is"
                " not worth it"
            )


def bracket_split_build_line(
    leaves: List[Leaf],
    original: Line,
    opening_bracket: Leaf,
    *,
    component: _BracketSplitComponent,
) -> Line:
    """Return a new line with given `leaves` and respective comments from `original`.

    If it's the head component, brackets will be tracked so trailing commas are
    respected.

    If it's the body component, the result line is one-indented inside brackets and as
    such has its first leaf's prefix normalized and a trailing comma added when
    expected.
    """
    result = Line(mode=original.mode, depth=original.depth)
    if component is _BracketSplitComponent.body:
        result.inside_brackets = True
        result.depth += 1
        if leaves:
            # Since body is a new indent level, remove spurious leading whitespace.
            normalize_prefix(leaves[0], inside_brackets=True)
            # Ensure a trailing comma for imports and standalone function arguments, but
            # be careful not to add one after any comments or within type annotations.
            no_commas = (
                original.is_def
                and opening_bracket.value == "("
                and not any(leaf.type == token.COMMA for leaf in leaves)
                # In particular, don't add one within a parenthesized return annotation.
                # Unfortunately the indicator we're in a return annotation (RARROW) may
                # be defined directly in the parent node, the parent of the parent ...
                # and so on depending on how complex the return annotation is.
                # This isn't perfect and there's some false negatives but they are in
                # contexts were a comma is actually fine.
                and not any(
                    node.prev_sibling.type == RARROW
                    for node in (
                        leaves[0].parent,
                        getattr(leaves[0].parent, "parent", None),
                    )
                    if isinstance(node, Node) and isinstance(node.prev_sibling, Leaf)
                )
            )

            if original.is_import or no_commas:
                for i in range(len(leaves) - 1, -1, -1):
                    if leaves[i].type == STANDALONE_COMMENT:
                        continue

                    if leaves[i].type != token.COMMA:
                        new_comma = Leaf(token.COMMA, ",")
                        leaves.insert(i + 1, new_comma)
                    break

    leaves_to_track: Set[LeafID] = set()
    if (
        Preview.handle_trailing_commas_in_head in original.mode
        and component is _BracketSplitComponent.head
    ):
        leaves_to_track = get_leaves_inside_matching_brackets(leaves)
    # Populate the line
    for leaf in leaves:
        result.append(
            leaf,
            preformatted=True,
            track_bracket=id(leaf) in leaves_to_track,
        )
        for comment_after in original.comments_after(leaf):
            result.append(comment_after, preformatted=True)
    if component is _BracketSplitComponent.body and should_split_line(
        result, opening_bracket
    ):
        result.should_split_rhs = True
    return result


def dont_increase_indentation(split_func: Transformer) -> Transformer:
    """Normalize prefix of the first leaf in every line returned by `split_func`.

    This is a decorator over relevant split functions.
    """

    @wraps(split_func)
    def split_wrapper(line: Line, features: Collection[Feature] = ()) -> Iterator[Line]:
        for split_line in split_func(line, features):
            normalize_prefix(split_line.leaves[0], inside_brackets=True)
            yield split_line

    return split_wrapper


@dont_increase_indentation
def delimiter_split(line: Line, features: Collection[Feature] = ()) -> Iterator[Line]:
    """Split according to delimiters of the highest priority.

    If the appropriate Features are given, the split will add trailing commas
    also in function signatures and calls that contain `*` and `**`.
    """
    try:
        last_leaf = line.leaves[-1]
    except IndexError:
        raise CannotSplit("Line empty") from None

    bt = line.bracket_tracker
    try:
        delimiter_priority = bt.max_delimiter_priority(exclude={id(last_leaf)})
    except ValueError:
        raise CannotSplit("No delimiters found") from None

    if delimiter_priority == DOT_PRIORITY:
        if bt.delimiter_count_with_priority(delimiter_priority) == 1:
            raise CannotSplit("Splitting a single attribute from its owner looks wrong")

    current_line = Line(
        mode=line.mode, depth=line.depth, inside_brackets=line.inside_brackets
    )
    lowest_depth = sys.maxsize
    trailing_comma_safe = True

    def append_to_line(leaf: Leaf) -> Iterator[Line]:
        """Append `leaf` to current line or to new line if appending impossible."""
        nonlocal current_line
        try:
            current_line.append_safe(leaf, preformatted=True)
        except ValueError:
            yield current_line

            current_line = Line(
                mode=line.mode, depth=line.depth, inside_brackets=line.inside_brackets
            )
            current_line.append(leaf)

    for leaf in line.leaves:
        yield from append_to_line(leaf)

        for comment_after in line.comments_after(leaf):
            yield from append_to_line(comment_after)

        lowest_depth = min(lowest_depth, leaf.bracket_depth)
        if leaf.bracket_depth == lowest_depth:
            if is_vararg(leaf, within={syms.typedargslist}):
                trailing_comma_safe = (
                    trailing_comma_safe and Feature.TRAILING_COMMA_IN_DEF in features
                )
            elif is_vararg(leaf, within={syms.arglist, syms.argument}):
                trailing_comma_safe = (
                    trailing_comma_safe and Feature.TRAILING_COMMA_IN_CALL in features
                )

        leaf_priority = bt.delimiters.get(id(leaf))
        if leaf_priority == delimiter_priority:
            yield current_line

            current_line = Line(
                mode=line.mode, depth=line.depth, inside_brackets=line.inside_brackets
            )
    if current_line:
        if (
            trailing_comma_safe
            and delimiter_priority == COMMA_PRIORITY
            and current_line.leaves[-1].type != token.COMMA
            and current_line.leaves[-1].type != STANDALONE_COMMENT
        ):
            new_comma = Leaf(token.COMMA, ",")
            current_line.append(new_comma)
        yield current_line


@dont_increase_indentation
def standalone_comment_split(
    line: Line, features: Collection[Feature] = ()
) -> Iterator[Line]:
    """Split standalone comments from the rest of the line."""
    if not line.contains_standalone_comments(0):
        raise CannotSplit("Line does not have any standalone comments")

    current_line = Line(
        mode=line.mode, depth=line.depth, inside_brackets=line.inside_brackets
    )

    def append_to_line(leaf: Leaf) -> Iterator[Line]:
        """Append `leaf` to current line or to new line if appending impossible."""
        nonlocal current_line
        try:
            current_line.append_safe(leaf, preformatted=True)
        except ValueError:
            yield current_line

            current_line = Line(
                line.mode, depth=line.depth, inside_brackets=line.inside_brackets
            )
            current_line.append(leaf)

    for leaf in line.leaves:
        yield from append_to_line(leaf)

        for comment_after in line.comments_after(leaf):
            yield from append_to_line(comment_after)

    if current_line:
        yield current_line


def normalize_prefix(leaf: Leaf, *, inside_brackets: bool) -> None:
    """Leave existing extra newlines if not `inside_brackets`. Remove everything
    else.

    Note: don't use backslashes for formatting or you'll lose your voting rights.
    """
    if not inside_brackets:
        spl = leaf.prefix.split("#")
        if "\\" not in spl[0]:
            nl_count = spl[-1].count("\n")
            if len(spl) > 1:
                nl_count -= 1
            leaf.prefix = "\n" * nl_count
            return

    leaf.prefix = ""


def normalize_invisible_parens(
    node: Node, parens_after: Set[str], *, preview: bool
) -> None:
    """Make existing optional parentheses invisible or create new ones.

    `parens_after` is a set of string leaf values immediately after which parens
    should be put.

    Standardizes on visible parentheses for single-element tuples, and keeps
    existing visible parentheses for other tuples and generator expressions.
    """
    for pc in list_comments(node.prefix, is_endmarker=False, preview=preview):
        if pc.value in FMT_OFF:
            # This `node` has a prefix with `# fmt: off`, don't mess with parens.
            return
    check_lpar = False
    for index, child in enumerate(list(node.children)):
        # Fixes a bug where invisible parens are not properly stripped from
        # assignment statements that contain type annotations.
        if isinstance(child, Node) and child.type == syms.annassign:
            normalize_invisible_parens(
                child, parens_after=parens_after, preview=preview
            )

        # Add parentheses around long tuple unpacking in assignments.
        if (
            index == 0
            and isinstance(child, Node)
            and child.type == syms.testlist_star_expr
        ):
            check_lpar = True

        if check_lpar:
            if (
                preview
                and child.type == syms.atom
                and node.type == syms.for_stmt
                and isinstance(child.prev_sibling, Leaf)
                and child.prev_sibling.type == token.NAME
                and child.prev_sibling.value == "for"
            ):
                if maybe_make_parens_invisible_in_atom(
                    child,
                    parent=node,
                    remove_brackets_around_comma=True,
                ):
                    wrap_in_parentheses(node, child, visible=False)
            elif preview and isinstance(child, Node) and node.type == syms.with_stmt:
                remove_with_parens(child, node)
            elif child.type == syms.atom:
                if maybe_make_parens_invisible_in_atom(
                    child,
                    parent=node,
                ):
                    wrap_in_parentheses(node, child, visible=False)
            elif is_one_tuple(child):
                wrap_in_parentheses(node, child, visible=True)
            elif node.type == syms.import_from:
                # "import from" nodes store parentheses directly as part of
                # the statement
                if is_lpar_token(child):
                    assert is_rpar_token(node.children[-1])
                    # make parentheses invisible
                    child.value = ""
                    node.children[-1].value = ""
                elif child.type != token.STAR:
                    # insert invisible parentheses
                    node.insert_child(index, Leaf(token.LPAR, ""))
                    node.append_child(Leaf(token.RPAR, ""))
                break
            elif (
                index == 1
                and child.type == token.STAR
                and node.type == syms.except_clause
            ):
                # In except* (PEP 654), the star is actually part of
                # of the keyword. So we need to skip the insertion of
                # invisible parentheses to work more precisely.
                continue

            elif not (isinstance(child, Leaf) and is_multiline_string(child)):
                wrap_in_parentheses(node, child, visible=False)

        comma_check = child.type == token.COMMA if preview else False

        check_lpar = isinstance(child, Leaf) and (
            child.value in parens_after or comma_check
        )


def remove_await_parens(node: Node) -> None:
    if node.children[0].type == token.AWAIT and len(node.children) > 1:
        if (
            node.children[1].type == syms.atom
            and node.children[1].children[0].type == token.LPAR
        ):
            if maybe_make_parens_invisible_in_atom(
                node.children[1],
                parent=node,
                remove_brackets_around_comma=True,
            ):
                wrap_in_parentheses(node, node.children[1], visible=False)

            # Since await is an expression we shouldn't remove
            # brackets in cases where this would change
            # the AST due to operator precedence.
            # Therefore we only aim to remove brackets around
            # power nodes that aren't also await expressions themselves.
            # https://peps.python.org/pep-0492/#updated-operator-precedence-table
            # N.B. We've still removed any redundant nested brackets though :)
            opening_bracket = cast(Leaf, node.children[1].children[0])
            closing_bracket = cast(Leaf, node.children[1].children[-1])
            bracket_contents = cast(Node, node.children[1].children[1])
            if bracket_contents.type != syms.power:
                ensure_visible(opening_bracket)
                ensure_visible(closing_bracket)
            elif (
                bracket_contents.type == syms.power
                and bracket_contents.children[0].type == token.AWAIT
            ):
                ensure_visible(opening_bracket)
                ensure_visible(closing_bracket)
                # If we are in a nested await then recurse down.
                remove_await_parens(bracket_contents)


def remove_with_parens(node: Node, parent: Node) -> None:
    """Recursively hide optional parens in `with` statements."""
    # Removing all unnecessary parentheses in with statements in one pass is a tad
    # complex as different variations of bracketed statements result in pretty
    # different parse trees:
    #
    # with (open("file")) as f:                       # this is an asexpr_test
    #     ...
    #
    # with (open("file") as f):                       # this is an atom containing an
    #     ...                                         # asexpr_test
    #
    # with (open("file")) as f, (open("file")) as f:  # this is asexpr_test, COMMA,
    #     ...                                         # asexpr_test
    #
    # with (open("file") as f, open("file") as f):    # an atom containing a
    #     ...                                         # testlist_gexp which then
    #                                                 # contains multiple asexpr_test(s)
    if node.type == syms.atom:
        if maybe_make_parens_invisible_in_atom(
            node,
            parent=parent,
            remove_brackets_around_comma=True,
        ):
            wrap_in_parentheses(parent, node, visible=False)
        if isinstance(node.children[1], Node):
            remove_with_parens(node.children[1], node)
    elif node.type == syms.testlist_gexp:
        for child in node.children:
            if isinstance(child, Node):
                remove_with_parens(child, node)
    elif node.type == syms.asexpr_test and not any(
        leaf.type == token.COLONEQUAL for leaf in node.leaves()
    ):
        if maybe_make_parens_invisible_in_atom(
            node.children[0],
            parent=node,
            remove_brackets_around_comma=True,
        ):
            wrap_in_parentheses(node, node.children[0], visible=False)


def maybe_make_parens_invisible_in_atom(
    node: LN,
    parent: LN,
    remove_brackets_around_comma: bool = False,
) -> bool:
    """If it's safe, make the parens in the atom `node` invisible, recursively.
    Additionally, remove repeated, adjacent invisible parens from the atom `node`
    as they are redundant.

    Returns whether the node should itself be wrapped in invisible parentheses.
    """
    if (
        node.type != syms.atom
        or is_empty_tuple(node)
        or is_one_tuple(node)
        or (is_yield(node) and parent.type != syms.expr_stmt)
        or (
            # This condition tries to prevent removing non-optional brackets
            # around a tuple, however, can be a bit overzealous so we provide
            # and option to skip this check for `for` and `with` statements.
            not remove_brackets_around_comma
            and max_delimiter_priority_in_atom(node) >= COMMA_PRIORITY
        )
    ):
        return False

    if is_walrus_assignment(node):
        if parent.type in [
            syms.annassign,
            syms.expr_stmt,
            syms.assert_stmt,
            syms.return_stmt,
            # these ones aren't useful to end users, but they do please fuzzers
            syms.for_stmt,
            syms.del_stmt,
        ]:
            return False

    first = node.children[0]
    last = node.children[-1]
    if is_lpar_token(first) and is_rpar_token(last):
        middle = node.children[1]
        # make parentheses invisible
        first.value = ""
        last.value = ""
        maybe_make_parens_invisible_in_atom(
            middle,
            parent=parent,
            remove_brackets_around_comma=remove_brackets_around_comma,
        )

        if is_atom_with_invisible_parens(middle):
            # Strip the invisible parens from `middle` by replacing
            # it with the child in-between the invisible parens
            middle.replace(middle.children[1])

        return False

    return True


def should_split_line(line: Line, opening_bracket: Leaf) -> bool:
    """Should `line` be immediately split with `delimiter_split()` after RHS?"""

    if not (opening_bracket.parent and opening_bracket.value in "[{("):
        return False

    # We're essentially checking if the body is delimited by commas and there's more
    # than one of them (we're excluding the trailing comma and if the delimiter priority
    # is still commas, that means there's more).
    exclude = set()
    trailing_comma = False
    try:
        last_leaf = line.leaves[-1]
        if last_leaf.type == token.COMMA:
            trailing_comma = True
            exclude.add(id(last_leaf))
        max_priority = line.bracket_tracker.max_delimiter_priority(exclude=exclude)
    except (IndexError, ValueError):
        return False

    return max_priority == COMMA_PRIORITY and (
        (line.mode.magic_trailing_comma and trailing_comma)
        # always explode imports
        or opening_bracket.parent.type in {syms.atom, syms.import_from}
    )


def generate_trailers_to_omit(line: Line, line_length: int) -> Iterator[Set[LeafID]]:
    """Generate sets of closing bracket IDs that should be omitted in a RHS.

    Brackets can be omitted if the entire trailer up to and including
    a preceding closing bracket fits in one line.

    Yielded sets are cumulative (contain results of previous yields, too).  First
    set is empty, unless the line should explode, in which case bracket pairs until
    the one that needs to explode are omitted.
    """

    omit: Set[LeafID] = set()
    if not line.magic_trailing_comma:
        yield omit

    length = 4 * line.depth
    opening_bracket: Optional[Leaf] = None
    closing_bracket: Optional[Leaf] = None
    inner_brackets: Set[LeafID] = set()
    for index, leaf, leaf_length in line.enumerate_with_length(reversed=True):
        length += leaf_length
        if length > line_length:
            break

        has_inline_comment = leaf_length > len(leaf.value) + len(leaf.prefix)
        if leaf.type == STANDALONE_COMMENT or has_inline_comment:
            break

        if opening_bracket:
            if leaf is opening_bracket:
                opening_bracket = None
            elif leaf.type in CLOSING_BRACKETS:
                prev = line.leaves[index - 1] if index > 0 else None
                if (
                    prev
                    and prev.type == token.COMMA
                    and leaf.opening_bracket is not None
                    and not is_one_sequence_between(
                        leaf.opening_bracket, leaf, line.leaves
                    )
                ):
                    # Never omit bracket pairs with trailing commas.
                    # We need to explode on those.
                    break

                inner_brackets.add(id(leaf))
        elif leaf.type in CLOSING_BRACKETS:
            prev = line.leaves[index - 1] if index > 0 else None
            if prev and prev.type in OPENING_BRACKETS:
                # Empty brackets would fail a split so treat them as "inner"
                # brackets (e.g. only add them to the `omit` set if another
                # pair of brackets was good enough.
                inner_brackets.add(id(leaf))
                continue

            if closing_bracket:
                omit.add(id(closing_bracket))
                omit.update(inner_brackets)
                inner_brackets.clear()
                yield omit

            if (
                prev
                and prev.type == token.COMMA
                and leaf.opening_bracket is not None
                and not is_one_sequence_between(leaf.opening_bracket, leaf, line.leaves)
            ):
                # Never omit bracket pairs with trailing commas.
                # We need to explode on those.
                break

            if leaf.value:
                opening_bracket = leaf.opening_bracket
                closing_bracket = leaf


def run_transformer(
    line: Line,
    transform: Transformer,
    mode: Mode,
    features: Collection[Feature],
    *,
    line_str: str = "",
) -> List[Line]:
    if not line_str:
        line_str = line_to_string(line)
    result: List[Line] = []
    for transformed_line in transform(line, features):
        if str(transformed_line).strip("\n") == line_str:
            raise CannotTransform("Line transformer returned an unchanged result")

        result.extend(transform_line(transformed_line, mode=mode, features=features))

    if (
        transform.__class__.__name__ != "rhs"
        or not line.bracket_tracker.invisible
        or any(bracket.value for bracket in line.bracket_tracker.invisible)
        or line.contains_multiline_strings()
        or result[0].contains_uncollapsable_type_comments()
        or result[0].contains_unsplittable_type_ignore()
        or is_line_short_enough(result[0], line_length=mode.line_length)
        # If any leaves have no parents (which _can_ occur since
        # `transform(line)` potentially destroys the line's underlying node
        # structure), then we can't proceed. Doing so would cause the below
        # call to `append_leaves()` to fail.
        or any(leaf.parent is None for leaf in line.leaves)
    ):
        return result

    line_copy = line.clone()
    append_leaves(line_copy, line, line.leaves)
    features_fop = set(features) | {Feature.FORCE_OPTIONAL_PARENTHESES}
    second_opinion = run_transformer(
        line_copy, transform, mode, features_fop, line_str=line_str
    )
    if all(
        is_line_short_enough(ln, line_length=mode.line_length) for ln in second_opinion
    ):
        result = second_opinion
    return result
