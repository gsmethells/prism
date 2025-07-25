import io
import json
import platform
import re
import sys
import tokenize
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from enum import Enum
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import (
    Any,
    Dict,
    Generator,
    Iterator,
    List,
    MutableMapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Sized,
    Tuple,
    Union,
)

import click
from click.core import ParameterSource
from mypy_extensions import mypyc_attr
from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPatternError

from _prism_version import version as __version__
from prism.cache import Cache, get_cache_info, read_cache, write_cache
from prism.comments import normalize_fmt_off
from prism.const import (
    DEFAULT_EXCLUDES,
    DEFAULT_INCLUDES,
    DEFAULT_LINE_LENGTH,
    STDIN_PLACEHOLDER,
)
from prism.files import (
    find_project_root,
    find_pyproject_toml,
    find_user_pyproject_toml,
    gen_python_files,
    get_gitignore,
    normalize_path_maybe_ignore,
    parse_pyproject_toml,
    wrap_stream_for_windows,
)
from prism.handle_ipynb_magics import (
    PYTHON_CELL_MAGICS,
    TRANSFORMED_MAGICS,
    jupyter_dependencies_are_installed,
    mask_cell,
    put_trailing_semicolon_back,
    remove_trailing_semicolon,
    unmask_cell,
)
from prism.linegen import LN, LineGenerator, transform_line
from prism.lines import EmptyLineTracker, LinesBlock
from prism.mode import (
    FUTURE_FLAG_TO_FEATURE,
    VERSION_TO_FEATURES,
    Feature,
    Mode,
    TargetVersion,
    supports_feature,
)
from prism.nodes import (
    STARS,
    is_number_token,
    is_simple_decorator_expression,
    is_string_token,
    syms,
)
from prism.output import color_diff, diff, dump_to_file, err, ipynb_diff, out
from prism.parsing import InvalidInput  # noqa F401
from prism.parsing import lib2to3_parse, parse_ast, stringify_ast
from prism.report import Changed, NothingChanged, Report
from prism.trans import iter_fexpr_spans
from blib2to3.pgen2 import token
from blib2to3.pytree import Leaf, Node

COMPILED = Path(__file__).suffix in (".pyd", ".so")

# types
FileContent = str
Encoding = str
NewLine = str


class WriteBack(Enum):
    NO = 0
    YES = 1
    DIFF = 2
    CHECK = 3
    COLOR_DIFF = 4

    @classmethod
    def from_configuration(
        cls, *, check: bool, diff: bool, color: bool = False
    ) -> "WriteBack":
        if check and not diff:
            return cls.CHECK

        if diff and color:
            return cls.COLOR_DIFF

        return cls.DIFF if diff else cls.YES


# Legacy name, left for integrations.
FileMode = Mode


def read_pyproject_toml(
    ctx: click.Context, param: click.Parameter, value: Optional[str]
) -> Optional[str]:
    """Inject Prism configuration from "pyproject.toml" into defaults in `ctx`.

    Returns the path to a successfully found and read configuration file, None
    otherwise.
    """
    if not value:
        value = find_pyproject_toml(ctx.params.get("src", ()))
        if value is None:
            return None

    try:
        config = parse_pyproject_toml(value)
    except (OSError, ValueError) as e:
        raise click.FileError(
            filename=value, hint=f"Error reading configuration file: {e}"
        ) from None

    if not config:
        return None
    else:
        # Sanitize the values to be Click friendly. For more information please see:
        # https://github.com/psf/prism/issues/1458
        # https://github.com/pallets/click/issues/1567
        config = {
            k: str(v) if not isinstance(v, (list, dict)) else v
            for k, v in config.items()
        }

    target_version = config.get("target_version")
    if target_version is not None and not isinstance(target_version, list):
        raise click.BadOptionUsage(
            "target-version", "Config key target-version must be a list"
        )

    default_map: Dict[str, Any] = {}
    if ctx.default_map:
        default_map.update(ctx.default_map)
    default_map.update(config)

    ctx.default_map = default_map
    return value


def target_version_option_callback(
    c: click.Context, p: Union[click.Option, click.Parameter], v: Tuple[str, ...]
) -> List[TargetVersion]:
    """Compute the target versions from a --target-version flag.

    This is its own function because mypy couldn't infer the type correctly
    when it was a lambda, causing mypyc trouble.
    """
    return [TargetVersion[val.upper()] for val in v]


def re_compile_maybe_verbose(regex: str) -> Pattern[str]:
    """Compile a regular expression string in `regex`.

    If it contains newlines, use verbose mode.
    """
    if "\n" in regex:
        regex = "(?x)" + regex
    compiled: Pattern[str] = re.compile(regex)
    return compiled


def validate_regex(
    ctx: click.Context,
    param: click.Parameter,
    value: Optional[str],
) -> Optional[Pattern[str]]:
    try:
        return re_compile_maybe_verbose(value) if value is not None else None
    except re.error as e:
        raise click.BadParameter(f"Not a valid regular expression: {e}") from None


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    # While Click does set this field automatically using the docstring, mypyc
    # (annoyingly) strips 'em so we need to set it here too.
    help="The uncompromising code formatter.",
)
@click.option("-c", "--code", type=str, help="Format the code passed in as a string.")
@click.option(
    "-l",
    "--line-length",
    type=int,
    default=DEFAULT_LINE_LENGTH,
    help="How many characters per line to allow.",
    show_default=True,
)
@click.option(
    "-t",
    "--target-version",
    type=click.Choice([v.name.lower() for v in TargetVersion]),
    callback=target_version_option_callback,
    multiple=True,
    help=(
        "Python versions that should be supported by Prism's output. [default: per-file"
        " auto-detection]"
    ),
)
@click.option(
    "--pyi",
    is_flag=True,
    help=(
        "Format all input files like typing stubs regardless of file extension (useful"
        " when piping source on standard input)."
    ),
)
@click.option(
    "--ipynb",
    is_flag=True,
    help=(
        "Format all input files like Jupyter Notebooks regardless of file extension "
        "(useful when piping source on standard input)."
    ),
)
@click.option(
    "--python-cell-magics",
    multiple=True,
    help=(
        "When processing Jupyter Notebooks, add the given magic to the list"
        f" of known python-magics ({', '.join(PYTHON_CELL_MAGICS)})."
        " Useful for formatting cells with custom python magics."
    ),
    default=[],
)
@click.option(
    "-x",
    "--skip-source-first-line",
    is_flag=True,
    help="Skip the first line of the source code.",
)
@click.option(
    "-S",
    "--skip-string-normalization",
    is_flag=True,
    help="Don't normalize string quotes or prefixes.",
)
@click.option(
    "-C",
    "--skip-magic-trailing-comma",
    is_flag=True,
    help="Don't use trailing commas as a reason to split lines.",
)
@click.option(
    "--experimental-string-processing",
    is_flag=True,
    hidden=True,
    help="(DEPRECATED and now included in --preview) Normalize string literals.",
)
@click.option(
    "--preview",
    is_flag=True,
    help=(
        "Enable potentially disruptive style changes that may be added to Prism's main"
        " functionality in the next major release."
    ),
)
@click.option(
    "--check",
    is_flag=True,
    help=(
        "Don't write the files back, just return the status. Return code 0 means"
        " nothing would change. Return code 1 means some files would be reformatted."
        " Return code 123 means there was an internal error."
    ),
)
@click.option(
    "--diff",
    is_flag=True,
    help="Don't write the files back, just output a diff for each file on stdout.",
)
@click.option(
    "--color/--no-color",
    is_flag=True,
    help="Show colored diff. Only applies when `--diff` is given.",
)
@click.option(
    "--fast/--safe",
    is_flag=True,
    help="If --fast given, skip temporary sanity checks. [default: --safe]",
)
@click.option(
    "--required-version",
    type=str,
    help=(
        "Require a specific version of Prism to be running (useful for unifying results"
        " across many environments e.g. with a pyproject.toml file). It can be"
        " either a major version number or an exact version."
    ),
)
@click.option(
    "--include",
    type=str,
    default=DEFAULT_INCLUDES,
    callback=validate_regex,
    help=(
        "A regular expression that matches files and directories that should be"
        " included on recursive searches. An empty value means all files are included"
        " regardless of the name. Use forward slashes for directories on all platforms"
        " (Windows, too). Exclusions are calculated first, inclusions later."
    ),
    show_default=True,
)
@click.option(
    "--exclude",
    type=str,
    callback=validate_regex,
    help=(
        "A regular expression that matches files and directories that should be"
        " excluded on recursive searches. An empty value means no paths are excluded."
        " Use forward slashes for directories on all platforms (Windows, too)."
        " Exclusions are calculated first, inclusions later. [default:"
        f" {DEFAULT_EXCLUDES}]"
    ),
    show_default=False,
)
@click.option(
    "--extend-exclude",
    type=str,
    callback=validate_regex,
    help=(
        "Like --exclude, but adds additional files and directories on top of the"
        " excluded ones. (Useful if you simply want to add to the default)"
    ),
)
@click.option(
    "--force-exclude",
    type=str,
    callback=validate_regex,
    help=(
        "Like --exclude, but files and directories matching this regex will be "
        "excluded even when they are passed explicitly as arguments."
    ),
)
@click.option(
    "--stdin-filename",
    type=str,
    help=(
        "The name of the file when passing it through stdin. Useful to make "
        "sure Prism will respect --force-exclude option on some "
        "editors that rely on using stdin."
    ),
)
@click.option(
    "-W",
    "--workers",
    type=click.IntRange(min=1),
    default=None,
    help="Number of parallel workers [default: number of CPUs in the system]",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help=(
        "Don't emit non-error messages to stderr. Errors are still emitted; silence"
        " those with 2>/dev/null."
    ),
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help=(
        "Also emit messages to stderr about files that were not changed or were ignored"
        " due to exclusion patterns."
    ),
)
@click.version_option(
    version=__version__,
    message=(
        f"%(prog)s, %(version)s (compiled: {'yes' if COMPILED else 'no'})\n"
        f"Python ({platform.python_implementation()}) {platform.python_version()}"
    ),
)
@click.argument(
    "src",
    nargs=-1,
    type=click.Path(
        exists=True, file_okay=True, dir_okay=True, readable=True, allow_dash=True
    ),
    is_eager=True,
    metavar="SRC ...",
)
@click.option(
    "--config",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        allow_dash=False,
        path_type=str,
    ),
    is_eager=True,
    callback=read_pyproject_toml,
    help="Read configuration from FILE path.",
)
@click.pass_context
def main(  # noqa: C901
    ctx: click.Context,
    code: Optional[str],
    line_length: int,
    target_version: List[TargetVersion],
    check: bool,
    diff: bool,
    color: bool,
    fast: bool,
    pyi: bool,
    ipynb: bool,
    python_cell_magics: Sequence[str],
    skip_source_first_line: bool,
    skip_string_normalization: bool,
    skip_magic_trailing_comma: bool,
    experimental_string_processing: bool,
    preview: bool,
    quiet: bool,
    verbose: bool,
    required_version: Optional[str],
    include: Pattern[str],
    exclude: Optional[Pattern[str]],
    extend_exclude: Optional[Pattern[str]],
    force_exclude: Optional[Pattern[str]],
    stdin_filename: Optional[str],
    workers: Optional[int],
    src: Tuple[str, ...],
    config: Optional[str],
) -> None:
    """The uncompromising code formatter."""
    ctx.ensure_object(dict)

    if src and code is not None:
        out(
            main.get_usage(ctx)
            + "\n\n'SRC' and 'code' cannot be passed simultaneously."
        )
        ctx.exit(1)
    if not src and code is None:
        out(main.get_usage(ctx) + "\n\nOne of 'SRC' or 'code' is required.")
        ctx.exit(1)

    root, method = (
        find_project_root(src, stdin_filename) if code is None else (None, None)
    )
    ctx.obj["root"] = root

    if verbose:
        if root:
            out(
                f"Identified `{root}` as project root containing a {method}.",
                fg="blue",
            )

            normalized = [
                (source, source)
                if source == "-"
                else (normalize_path_maybe_ignore(Path(source), root), source)
                for source in src
            ]
            srcs_string = ", ".join(
                [
                    f'"{_norm}"'
                    if _norm
                    else f'\033[31m"{source} (skipping - invalid)"\033[34m'
                    for _norm, source in normalized
                ]
            )
            out(f"Sources to be formatted: {srcs_string}", fg="blue")

        if config:
            config_source = ctx.get_parameter_source("config")
            user_level_config = str(find_user_pyproject_toml())
            if config == user_level_config:
                out(
                    (
                        "Using configuration from user-level config at "
                        f"'{user_level_config}'."
                    ),
                    fg="blue",
                )
            elif config_source in (
                ParameterSource.DEFAULT,
                ParameterSource.DEFAULT_MAP,
            ):
                out("Using configuration from project root.", fg="blue")
            else:
                out(f"Using configuration in '{config}'.", fg="blue")

    error_msg = "Oh no! 💥 💔 💥"
    if (
        required_version
        and required_version != __version__
        and required_version != __version__.split(".")[0]
    ):
        err(
            f"{error_msg} The required version `{required_version}` does not match"
            f" the running version `{__version__}`!"
        )
        ctx.exit(1)
    if ipynb and pyi:
        err("Cannot pass both `pyi` and `ipynb` flags!")
        ctx.exit(1)

    write_back = WriteBack.from_configuration(check=check, diff=diff, color=color)
    if target_version:
        versions = set(target_version)
    else:
        # We'll autodetect later.
        versions = set()
    mode = Mode(
        target_versions=versions,
        line_length=line_length,
        is_pyi=pyi,
        is_ipynb=ipynb,
        skip_source_first_line=skip_source_first_line,
        string_normalization=not skip_string_normalization,
        magic_trailing_comma=not skip_magic_trailing_comma,
        experimental_string_processing=experimental_string_processing,
        preview=preview,
        python_cell_magics=set(python_cell_magics),
    )

    if code is not None:
        # Run in quiet mode by default with -c; the extra output isn't useful.
        # You can still pass -v to get verbose output.
        quiet = True

    report = Report(check=check, diff=diff, quiet=quiet, verbose=verbose)

    if code is not None:
        reformat_code(
            content=code, fast=fast, write_back=write_back, mode=mode, report=report
        )
    else:
        try:
            sources = get_sources(
                ctx=ctx,
                src=src,
                quiet=quiet,
                verbose=verbose,
                include=include,
                exclude=exclude,
                extend_exclude=extend_exclude,
                force_exclude=force_exclude,
                report=report,
                stdin_filename=stdin_filename,
            )
        except GitWildMatchPatternError:
            ctx.exit(1)

        path_empty(
            sources,
            "No Python files are present to be formatted. Nothing to do 😴",
            quiet,
            verbose,
            ctx,
        )

        if len(sources) == 1:
            reformat_one(
                src=sources.pop(),
                fast=fast,
                write_back=write_back,
                mode=mode,
                report=report,
            )
        else:
            from prism.concurrency import reformat_many

            reformat_many(
                sources=sources,
                fast=fast,
                write_back=write_back,
                mode=mode,
                report=report,
                workers=workers,
            )

    if verbose or not quiet:
        if code is None and (verbose or report.change_count or report.failure_count):
            out()
        out(error_msg if report.return_code else "All done! ✨ 🍰 ✨")
        if code is None:
            click.echo(str(report), err=True)
    ctx.exit(report.return_code)


def get_sources(
    *,
    ctx: click.Context,
    src: Tuple[str, ...],
    quiet: bool,
    verbose: bool,
    include: Pattern[str],
    exclude: Optional[Pattern[str]],
    extend_exclude: Optional[Pattern[str]],
    force_exclude: Optional[Pattern[str]],
    report: "Report",
    stdin_filename: Optional[str],
) -> Set[Path]:
    """Compute the set of files to be formatted."""
    sources: Set[Path] = set()
    root = ctx.obj["root"]

    using_default_exclude = exclude is None
    exclude = re_compile_maybe_verbose(DEFAULT_EXCLUDES) if exclude is None else exclude
    gitignore: Optional[PathSpec] = None
    root_gitignore = get_gitignore(root)

    for s in src:
        if s == "-" and stdin_filename:
            p = Path(stdin_filename)
            is_stdin = True
        else:
            p = Path(s)
            is_stdin = False

        if is_stdin or p.is_file():
            normalized_path = normalize_path_maybe_ignore(p, ctx.obj["root"], report)
            if normalized_path is None:
                continue

            normalized_path = "/" + normalized_path
            # Hard-exclude any files that matches the `--force-exclude` regex.
            if force_exclude:
                force_exclude_match = force_exclude.search(normalized_path)
            else:
                force_exclude_match = None
            if force_exclude_match and force_exclude_match.group(0):
                report.path_ignored(p, "matches the --force-exclude regular expression")
                continue

            if is_stdin:
                p = Path(f"{STDIN_PLACEHOLDER}{str(p)}")

            if p.suffix == ".ipynb" and not jupyter_dependencies_are_installed(
                verbose=verbose, quiet=quiet
            ):
                continue

            sources.add(p)
        elif p.is_dir():
            if using_default_exclude:
                gitignore = {
                    root: root_gitignore,
                    root / p: get_gitignore(p),
                }
            sources.update(
                gen_python_files(
                    p.iterdir(),
                    ctx.obj["root"],
                    include,
                    exclude,
                    extend_exclude,
                    force_exclude,
                    report,
                    gitignore,
                    verbose=verbose,
                    quiet=quiet,
                )
            )
        elif s == "-":
            sources.add(p)
        else:
            err(f"invalid path: {s}")
    return sources


def path_empty(
    src: Sized, msg: str, quiet: bool, verbose: bool, ctx: click.Context
) -> None:
    """
    Exit if there is no `src` provided for formatting
    """
    if not src:
        if verbose or not quiet:
            out(msg)
        ctx.exit(0)


def reformat_code(
    content: str, fast: bool, write_back: WriteBack, mode: Mode, report: Report
) -> None:
    """
    Reformat and print out `content` without spawning child processes.
    Similar to `reformat_one`, but for string content.

    `fast`, `write_back`, and `mode` options are passed to
    :func:`format_file_in_place` or :func:`format_stdin_to_stdout`.
    """
    path = Path("<string>")
    try:
        changed = Changed.NO
        if format_stdin_to_stdout(
            content=content, fast=fast, write_back=write_back, mode=mode
        ):
            changed = Changed.YES
        report.done(path, changed)
    except Exception as exc:
        if report.verbose:
            traceback.print_exc()
        report.failed(path, str(exc))


# diff-shades depends on being to monkeypatch this function to operate. I know it's
# not ideal, but this shouldn't cause any issues ... hopefully. ~ichard26
@mypyc_attr(patchable=True)
def reformat_one(
    src: Path, fast: bool, write_back: WriteBack, mode: Mode, report: "Report"
) -> None:
    """Reformat a single file under `src` without spawning child processes.

    `fast`, `write_back`, and `mode` options are passed to
    :func:`format_file_in_place` or :func:`format_stdin_to_stdout`.
    """
    try:
        changed = Changed.NO

        if str(src) == "-":
            is_stdin = True
        elif str(src).startswith(STDIN_PLACEHOLDER):
            is_stdin = True
            # Use the original name again in case we want to print something
            # to the user
            src = Path(str(src)[len(STDIN_PLACEHOLDER) :])
        else:
            is_stdin = False

        if is_stdin:
            if src.suffix == ".pyi":
                mode = replace(mode, is_pyi=True)
            elif src.suffix == ".ipynb":
                mode = replace(mode, is_ipynb=True)
            if format_stdin_to_stdout(fast=fast, write_back=write_back, mode=mode):
                changed = Changed.YES
        else:
            cache: Cache = {}
            if write_back not in (WriteBack.DIFF, WriteBack.COLOR_DIFF):
                cache = read_cache(mode)
                res_src = src.resolve()
                res_src_s = str(res_src)
                if res_src_s in cache and cache[res_src_s] == get_cache_info(res_src):
                    changed = Changed.CACHED
            if changed is not Changed.CACHED and format_file_in_place(
                src, fast=fast, write_back=write_back, mode=mode
            ):
                changed = Changed.YES
            if (write_back is WriteBack.YES and changed is not Changed.CACHED) or (
                write_back is WriteBack.CHECK and changed is Changed.NO
            ):
                write_cache(cache, [src], mode)
        report.done(src, changed)
    except Exception as exc:
        if report.verbose:
            traceback.print_exc()
        report.failed(src, str(exc))


def format_file_in_place(
    src: Path,
    fast: bool,
    mode: Mode,
    write_back: WriteBack = WriteBack.NO,
    lock: Any = None,  # multiprocessing.Manager().Lock() is some crazy proxy
) -> bool:
    """Format file under `src` path. Return True if changed.

    If `write_back` is DIFF, write a diff to stdout. If it is YES, write reformatted
    code to the file.
    `mode` and `fast` options are passed to :func:`format_file_contents`.
    """
    if src.suffix == ".pyi":
        mode = replace(mode, is_pyi=True)
    elif src.suffix == ".ipynb":
        mode = replace(mode, is_ipynb=True)

    then = datetime.utcfromtimestamp(src.stat().st_mtime)
    header = b""
    with open(src, "rb") as buf:
        if mode.skip_source_first_line:
            header = buf.readline()
        src_contents, encoding, newline = decode_bytes(buf.read())
    try:
        dst_contents = format_file_contents(src_contents, fast=fast, mode=mode)
    except NothingChanged:
        return False
    except JSONDecodeError:
        raise ValueError(
            f"File '{src}' cannot be parsed as valid Jupyter notebook."
        ) from None
    src_contents = header.decode(encoding) + src_contents
    dst_contents = header.decode(encoding) + dst_contents

    if write_back == WriteBack.YES:
        with open(src, "w", encoding=encoding, newline=newline) as f:
            f.write(dst_contents)
    elif write_back in (WriteBack.DIFF, WriteBack.COLOR_DIFF):
        now = datetime.utcnow()
        src_name = f"{src}\t{then} +0000"
        dst_name = f"{src}\t{now} +0000"
        if mode.is_ipynb:
            diff_contents = ipynb_diff(src_contents, dst_contents, src_name, dst_name)
        else:
            diff_contents = diff(src_contents, dst_contents, src_name, dst_name)

        if write_back == WriteBack.COLOR_DIFF:
            diff_contents = color_diff(diff_contents)

        with lock or nullcontext():
            f = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding=encoding,
                newline=newline,
                write_through=True,
            )
            f = wrap_stream_for_windows(f)
            f.write(diff_contents)
            f.detach()

    return True


def format_stdin_to_stdout(
    fast: bool,
    *,
    content: Optional[str] = None,
    write_back: WriteBack = WriteBack.NO,
    mode: Mode,
) -> bool:
    """Format file on stdin. Return True if changed.

    If content is None, it's read from sys.stdin.

    If `write_back` is YES, write reformatted code back to stdout. If it is DIFF,
    write a diff to stdout. The `mode` argument is passed to
    :func:`format_file_contents`.
    """
    then = datetime.utcnow()

    if content is None:
        src, encoding, newline = decode_bytes(sys.stdin.buffer.read())
    else:
        src, encoding, newline = content, "utf-8", ""

    dst = src
    try:
        dst = format_file_contents(src, fast=fast, mode=mode)
        return True

    except NothingChanged:
        return False

    finally:
        f = io.TextIOWrapper(
            sys.stdout.buffer, encoding=encoding, newline=newline, write_through=True
        )
        if write_back == WriteBack.YES:
            # Make sure there's a newline after the content
            if dst and dst[-1] != "\n":
                dst += "\n"
            f.write(dst)
        elif write_back in (WriteBack.DIFF, WriteBack.COLOR_DIFF):
            now = datetime.utcnow()
            src_name = f"STDIN\t{then} +0000"
            dst_name = f"STDOUT\t{now} +0000"
            d = diff(src, dst, src_name, dst_name)
            if write_back == WriteBack.COLOR_DIFF:
                d = color_diff(d)
                f = wrap_stream_for_windows(f)
            f.write(d)
        f.detach()


def check_stability_and_equivalence(
    src_contents: str, dst_contents: str, *, mode: Mode
) -> None:
    """Perform stability and equivalence checks.

    Raise AssertionError if source and destination contents are not
    equivalent, or if a second pass of the formatter would format the
    content differently.
    """
    assert_equivalent(src_contents, dst_contents)
    assert_stable(src_contents, dst_contents, mode=mode)


def format_file_contents(src_contents: str, *, fast: bool, mode: Mode) -> FileContent:
    """Reformat contents of a file and return new contents.

    If `fast` is False, additionally confirm that the reformatted code is
    valid by calling :func:`assert_equivalent` and :func:`assert_stable` on it.
    `mode` is passed to :func:`format_str`.
    """
    if not mode.preview and not src_contents.strip():
        raise NothingChanged

    if mode.is_ipynb:
        dst_contents = format_ipynb_string(src_contents, fast=fast, mode=mode)
    else:
        dst_contents = format_str(src_contents, mode=mode)
    if src_contents == dst_contents:
        raise NothingChanged

    if not fast and not mode.is_ipynb:
        # Jupyter notebooks will already have been checked above.
        check_stability_and_equivalence(src_contents, dst_contents, mode=mode)
    return dst_contents


def validate_cell(src: str, mode: Mode) -> None:
    """Check that cell does not already contain TransformerManager transformations,
    or non-Python cell magics, which might cause tokenizer_rt to break because of
    indentations.

    If a cell contains ``!ls``, then it'll be transformed to
    ``get_ipython().system('ls')``. However, if the cell originally contained
    ``get_ipython().system('ls')``, then it would get transformed in the same way:

        >>> TransformerManager().transform_cell("get_ipython().system('ls')")
        "get_ipython().system('ls')\n"
        >>> TransformerManager().transform_cell("!ls")
        "get_ipython().system('ls')\n"

    Due to the impossibility of safely roundtripping in such situations, cells
    containing transformed magics will be ignored.
    """
    if any(transformed_magic in src for transformed_magic in TRANSFORMED_MAGICS):
        raise NothingChanged
    if (
        src[:2] == "%%"
        and src.split()[0][2:] not in PYTHON_CELL_MAGICS | mode.python_cell_magics
    ):
        raise NothingChanged


def format_cell(src: str, *, fast: bool, mode: Mode) -> str:
    """Format code in given cell of Jupyter notebook.

    General idea is:

      - if cell has trailing semicolon, remove it;
      - if cell has IPython magics, mask them;
      - format cell;
      - reinstate IPython magics;
      - reinstate trailing semicolon (if originally present);
      - strip trailing newlines.

    Cells with syntax errors will not be processed, as they
    could potentially be automagics or multi-line magics, which
    are currently not supported.
    """
    validate_cell(src, mode)
    src_without_trailing_semicolon, has_trailing_semicolon = remove_trailing_semicolon(
        src
    )
    try:
        masked_src, replacements = mask_cell(src_without_trailing_semicolon)
    except SyntaxError:
        raise NothingChanged from None
    masked_dst = format_str(masked_src, mode=mode)
    if not fast:
        check_stability_and_equivalence(masked_src, masked_dst, mode=mode)
    dst_without_trailing_semicolon = unmask_cell(masked_dst, replacements)
    dst = put_trailing_semicolon_back(
        dst_without_trailing_semicolon, has_trailing_semicolon
    )
    dst = dst.rstrip("\n")
    if dst == src:
        raise NothingChanged from None
    return dst


def validate_metadata(nb: MutableMapping[str, Any]) -> None:
    """If notebook is marked as non-Python, don't format it.

    All notebook metadata fields are optional, see
    https://nbformat.readthedocs.io/en/latest/format_description.html. So
    if a notebook has empty metadata, we will try to parse it anyway.
    """
    language = nb.get("metadata", {}).get("language_info", {}).get("name", None)
    if language is not None and language != "python":
        raise NothingChanged from None


def format_ipynb_string(src_contents: str, *, fast: bool, mode: Mode) -> FileContent:
    """Format Jupyter notebook.

    Operate cell-by-cell, only on code cells, only for Python notebooks.
    If the ``.ipynb`` originally had a trailing newline, it'll be preserved.
    """
    if mode.preview and not src_contents:
        raise NothingChanged

    trailing_newline = src_contents[-1] == "\n"
    modified = False
    nb = json.loads(src_contents)
    validate_metadata(nb)
    for cell in nb["cells"]:
        if cell.get("cell_type", None) == "code":
            try:
                src = "".join(cell["source"])
                dst = format_cell(src, fast=fast, mode=mode)
            except NothingChanged:
                pass
            else:
                cell["source"] = dst.splitlines(keepends=True)
                modified = True
    if modified:
        dst_contents = json.dumps(nb, indent=1, ensure_ascii=False)
        if trailing_newline:
            dst_contents = dst_contents + "\n"
        return dst_contents
    else:
        raise NothingChanged


def format_str(src_contents: str, *, mode: Mode) -> str:
    """Reformat a string and return new contents.

    `mode` determines formatting options, such as how many characters per line are
    allowed.  Example:

    >>> import prism
    >>> print(prism.format_str("def f(arg:str='')->None:...", mode=prism.Mode()))
    def f(arg: str = "") -> None:
        ...

    A more complex example:

    >>> print(
    ...   prism.format_str(
    ...     "def f(arg:str='')->None: hey",
    ...     mode=prism.Mode(
    ...       target_versions={prism.TargetVersion.PY36},
    ...       line_length=10,
    ...       string_normalization=False,
    ...       is_pyi=False,
    ...     ),
    ...   ),
    ... )
    def f(
        arg: str = '',
    ) -> None:
        hey

    """
    dst_contents = _format_str_once(src_contents, mode=mode)
    # Forced second pass to work around optional trailing commas (becoming
    # forced trailing commas on pass 2) interacting differently with optional
    # parentheses.  Admittedly ugly.
    if src_contents != dst_contents:
        return _format_str_once(dst_contents, mode=mode)
    return dst_contents


def _format_str_once(src_contents: str, *, mode: Mode) -> str:
    src_node = lib2to3_parse(src_contents.lstrip(), mode.target_versions)
    dst_blocks: List[LinesBlock] = []
    if mode.target_versions:
        versions = mode.target_versions
    else:
        future_imports = get_future_imports(src_node)
        versions = detect_target_versions(src_node, future_imports=future_imports)

    normalize_fmt_off(src_node, preview=mode.preview)
    lines = LineGenerator(mode=mode)
    elt = EmptyLineTracker(mode=mode)
    split_line_features = {
        feature
        for feature in {Feature.TRAILING_COMMA_IN_CALL, Feature.TRAILING_COMMA_IN_DEF}
        if supports_feature(versions, feature)
    }
    block: Optional[LinesBlock] = None
    for current_line in lines.visit(src_node):
        block = elt.maybe_empty_lines(current_line)
        dst_blocks.append(block)
        for line in transform_line(
            current_line, mode=mode, features=split_line_features
        ):
            block.content_lines.append(str(line))
    if dst_blocks:
        dst_blocks[-1].after = 0
    dst_contents = []
    for block in dst_blocks:
        dst_contents.extend(block.all_lines())
    if mode.preview and not dst_contents:
        # Use decode_bytes to retrieve the correct source newline (CRLF or LF),
        # and check if normalized_content has more than one line
        normalized_content, _, newline = decode_bytes(src_contents.encode("utf-8"))
        if "\n" in normalized_content:
            return newline
        return ""
    return "".join(dst_contents)


def decode_bytes(src: bytes) -> Tuple[FileContent, Encoding, NewLine]:
    """Return a tuple of (decoded_contents, encoding, newline).

    `newline` is either CRLF or LF but `decoded_contents` is decoded with
    universal newlines (i.e. only contains LF).
    """
    srcbuf = io.BytesIO(src)
    encoding, lines = tokenize.detect_encoding(srcbuf.readline)
    if not lines:
        return "", encoding, "\n"

    newline = "\r\n" if b"\r\n" == lines[0][-2:] else "\n"
    srcbuf.seek(0)
    with io.TextIOWrapper(srcbuf, encoding) as tiow:
        return tiow.read(), encoding, newline


def get_features_used(  # noqa: C901
    node: Node, *, future_imports: Optional[Set[str]] = None
) -> Set[Feature]:
    """Return a set of (relatively) new Python features used in this file.

    Currently looking for:
    - f-strings;
    - self-documenting expressions in f-strings (f"{x=}");
    - underscores in numeric literals;
    - trailing commas after * or ** in function signatures and calls;
    - positional only arguments in function signatures and lambdas;
    - assignment expression;
    - relaxed decorator syntax;
    - usage of __future__ flags (annotations);
    - print / exec statements;
    """
    features: Set[Feature] = set()
    if future_imports:
        features |= {
            FUTURE_FLAG_TO_FEATURE[future_import]
            for future_import in future_imports
            if future_import in FUTURE_FLAG_TO_FEATURE
        }

    for n in node.pre_order():
        if is_string_token(n):
            value_head = n.value[:2]
            if value_head in {'f"', 'F"', "f'", "F'", "rf", "fr", "RF", "FR"}:
                features.add(Feature.F_STRINGS)
                if Feature.DEBUG_F_STRINGS not in features:
                    for span_beg, span_end in iter_fexpr_spans(n.value):
                        if n.value[span_beg : span_end - 1].rstrip().endswith("="):
                            features.add(Feature.DEBUG_F_STRINGS)
                            break

        elif is_number_token(n):
            if "_" in n.value:
                features.add(Feature.NUMERIC_UNDERSCORES)

        elif n.type == token.SLASH:
            if n.parent and n.parent.type in {
                syms.typedargslist,
                syms.arglist,
                syms.varargslist,
            }:
                features.add(Feature.POS_ONLY_ARGUMENTS)

        elif n.type == token.COLONEQUAL:
            features.add(Feature.ASSIGNMENT_EXPRESSIONS)

        elif n.type == syms.decorator:
            if len(n.children) > 1 and not is_simple_decorator_expression(
                n.children[1]
            ):
                features.add(Feature.RELAXED_DECORATORS)

        elif (
            n.type in {syms.typedargslist, syms.arglist}
            and n.children
            and n.children[-1].type == token.COMMA
        ):
            if n.type == syms.typedargslist:
                feature = Feature.TRAILING_COMMA_IN_DEF
            else:
                feature = Feature.TRAILING_COMMA_IN_CALL

            for ch in n.children:
                if ch.type in STARS:
                    features.add(feature)

                if ch.type == syms.argument:
                    for argch in ch.children:
                        if argch.type in STARS:
                            features.add(feature)

        elif (
            n.type in {syms.return_stmt, syms.yield_expr}
            and len(n.children) >= 2
            and n.children[1].type == syms.testlist_star_expr
            and any(child.type == syms.star_expr for child in n.children[1].children)
        ):
            features.add(Feature.UNPACKING_ON_FLOW)

        elif (
            n.type == syms.annassign
            and len(n.children) >= 4
            and n.children[3].type == syms.testlist_star_expr
        ):
            features.add(Feature.ANN_ASSIGN_EXTENDED_RHS)

        elif (
            n.type == syms.except_clause
            and len(n.children) >= 2
            and n.children[1].type == token.STAR
        ):
            features.add(Feature.EXCEPT_STAR)

        elif n.type in {syms.subscriptlist, syms.trailer} and any(
            child.type == syms.star_expr for child in n.children
        ):
            features.add(Feature.VARIADIC_GENERICS)

        elif (
            n.type == syms.tname_star
            and len(n.children) == 3
            and n.children[2].type == syms.star_expr
        ):
            features.add(Feature.VARIADIC_GENERICS)

    return features


def detect_target_versions(
    node: Node, *, future_imports: Optional[Set[str]] = None
) -> Set[TargetVersion]:
    """Detect the version to target based on the nodes used."""
    features = get_features_used(node, future_imports=future_imports)
    return {
        version for version in TargetVersion if features <= VERSION_TO_FEATURES[version]
    }


def get_future_imports(node: Node) -> Set[str]:
    """Return a set of __future__ imports in the file."""
    imports: Set[str] = set()

    def get_imports_from_children(children: List[LN]) -> Generator[str, None, None]:
        for child in children:
            if isinstance(child, Leaf):
                if child.type == token.NAME:
                    yield child.value

            elif child.type == syms.import_as_name:
                orig_name = child.children[0]
                assert isinstance(orig_name, Leaf), "Invalid syntax parsing imports"
                assert orig_name.type == token.NAME, "Invalid syntax parsing imports"
                yield orig_name.value

            elif child.type == syms.import_as_names:
                yield from get_imports_from_children(child.children)

            else:
                raise AssertionError("Invalid syntax parsing imports")

    for child in node.children:
        if child.type != syms.simple_stmt:
            break

        first_child = child.children[0]
        if isinstance(first_child, Leaf):
            # Continue looking if we see a docstring; otherwise stop.
            if (
                len(child.children) == 2
                and first_child.type == token.STRING
                and child.children[1].type == token.NEWLINE
            ):
                continue

            break

        elif first_child.type == syms.import_from:
            module_name = first_child.children[1]
            if not isinstance(module_name, Leaf) or module_name.value != "__future__":
                break

            imports |= set(get_imports_from_children(first_child.children[3:]))
        else:
            break

    return imports


def assert_equivalent(src: str, dst: str) -> None:
    """Raise AssertionError if `src` and `dst` aren't equivalent."""
    try:
        src_ast = parse_ast(src)
    except Exception as exc:
        raise AssertionError(
            "cannot use --safe with this file; failed to parse source file AST: "
            f"{exc}\n"
            "This could be caused by running Prism with an older Python version "
            "that does not support new syntax used in your source file."
        ) from exc

    try:
        dst_ast = parse_ast(dst)
    except Exception as exc:
        log = dump_to_file("".join(traceback.format_tb(exc.__traceback__)), dst)
        raise AssertionError(
            f"INTERNAL ERROR: Prism produced invalid code: {exc}. "
            "Please report a bug on https://github.com/psf/prism/issues.  "
            f"This invalid output might be helpful: {log}"
        ) from None

    src_ast_str = "\n".join(stringify_ast(src_ast))
    dst_ast_str = "\n".join(stringify_ast(dst_ast))
    if src_ast_str != dst_ast_str:
        log = dump_to_file(diff(src_ast_str, dst_ast_str, "src", "dst"))
        raise AssertionError(
            "INTERNAL ERROR: Prism produced code that is not equivalent to the"
            " source.  Please report a bug on "
            f"https://github.com/psf/prism/issues.  This diff might be helpful: {log}"
        ) from None


def assert_stable(src: str, dst: str, mode: Mode) -> None:
    """Raise AssertionError if `dst` reformats differently the second time."""
    # We shouldn't call format_str() here, because that formats the string
    # twice and may hide a bug where we bounce back and forth between two
    # versions.
    newdst = _format_str_once(dst, mode=mode)
    if dst != newdst:
        log = dump_to_file(
            str(mode),
            diff(src, dst, "source", "first pass"),
            diff(dst, newdst, "first pass", "second pass"),
        )
        raise AssertionError(
            "INTERNAL ERROR: Prism produced different code on the second pass of the"
            " formatter.  Please report a bug on https://github.com/psf/prism/issues."
            f"  This diff might be helpful: {log}"
        ) from None


@contextmanager
def nullcontext() -> Iterator[None]:
    """Return an empty context manager.

    To be used like `nullcontext` in Python 3.7.
    """
    yield


def patch_click() -> None:
    """Make Click not crash on Python 3.6 with LANG=C.

    On certain misconfigured environments, Python 3 selects the ASCII encoding as the
    default which restricts paths that it can access during the lifetime of the
    application.  Click refuses to work in this scenario by raising a RuntimeError.

    In case of Prism the likelihood that non-ASCII characters are going to be used in
    file paths is minimal since it's Python source code.  Moreover, this crash was
    spurious on Python 3.7 thanks to PEP 538 and PEP 540.
    """
    modules: List[Any] = []
    try:
        from click import core
    except ImportError:
        pass
    else:
        modules.append(core)
    try:
        # Removed in Click 8.1.0 and newer; we keep this around for users who have
        # older versions installed.
        from click import _unicodefun  # type: ignore
    except ImportError:
        pass
    else:
        modules.append(_unicodefun)

    for module in modules:
        if hasattr(module, "_verify_python3_env"):
            module._verify_python3_env = lambda: None
        if hasattr(module, "_verify_python_env"):
            module._verify_python_env = lambda: None


def patched_main() -> None:
    # PyInstaller patches multiprocessing to need freeze_support() even in non-Windows
    # environments so just assume we always need to call it if frozen.
    if getattr(sys, "frozen", False):
        from multiprocessing import freeze_support

        freeze_support()

    patch_click()
    main()


if __name__ == "__main__":
    patched_main()
