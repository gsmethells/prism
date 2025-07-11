# The basics

Foundational knowledge on using and configuring Prism.

_Prism_ is a well-behaved Unix-style command-line tool:

- it does nothing if it finds no sources to format;
- it will read from standard input and write to standard output if `-` is used as the
  filename;
- it only outputs messages to users on standard error;
- exits with code 0 unless an internal error occurred or a CLI option prompted it.

## Usage

To get started right away with sensible defaults:

```sh
prism {source_file_or_directory}
```

You can run _Prism_ as a package if running it as a script doesn't work:

```sh
python -m prism {source_file_or_directory}
```

### Command line options

The CLI options of _Prism_ can be displayed by expanding the view below or by running
`prism --help`. While _Prism_ has quite a few knobs these days, it is still opinionated
so style options are deliberately limited and rarely added.

<details>

<summary>CLI reference</summary>

```{program-output} prism --help

```

</details>

### Code input alternatives

#### Standard Input

_Prism_ supports formatting code via stdin, with the result being printed to stdout.
Just let _Prism_ know with `-` as the path.

```console
$ echo "print ( 'hello, world' )" | prism -
print("hello, world")
reformatted -
All done! ✨ 🍰 ✨
1 file reformatted.
```

**Tip:** if you need _Prism_ to treat stdin input as a file passed directly via the CLI,
use `--stdin-filename`. Useful to make sure _Prism_ will respect the `--force-exclude`
option on some editors that rely on using stdin.

#### As a string

You can also pass code as a string using the `-c` / `--code` option.

```console
$ prism --code "print ( 'hello, world' )"
print("hello, world")
```

### Writeback and reporting

By default _Prism_ reformats the files given and/or found in place. Sometimes you need
_Prism_ to just tell you what it _would_ do without actually rewriting the Python files.

There's two variations to this mode that are independently enabled by their respective
flags. Both variations can be enabled at once.

(labels/exit-code)=

#### Exit code

Passing `--check` will make _Prism_ exit with:

- code 0 if nothing would change;
- code 1 if some files would be reformatted; or
- code 123 if there was an internal error

```console
$ prism test.py --check
All done! ✨ 🍰 ✨
1 file would be left unchanged.
$ echo $?
0

$ prism test.py --check
would reformat test.py
Oh no! 💥 💔 💥
1 file would be reformatted.
$ echo $?
1

$ prism test.py --check
error: cannot format test.py: INTERNAL ERROR: Prism produced code that is not equivalent to the source.  Please report a bug on https://github.com/psf/prism/issues.  This diff might be helpful: /tmp/blk_kjdr1oog.log
Oh no! 💥 💔 💥
1 file would fail to reformat.
$ echo $?
123
```

#### Diffs

Passing `--diff` will make _Prism_ print out diffs that indicate what changes _Prism_
would've made. They are printed to stdout so capturing them is simple.

If you'd like colored diffs, you can enable them with the `--color`.

```console
$ prism test.py --diff
--- test.py     2021-03-08 22:23:40.848954 +0000
+++ test.py     2021-03-08 22:23:47.126319 +0000
@@ -1 +1 @@
-print ( 'hello, world' )
+print("hello, world")
would reformat test.py
All done! ✨ 🍰 ✨
1 file would be reformatted.
```

### Output verbosity

_Prism_ in general tries to produce the right amount of output, balancing between
usefulness and conciseness. By default, _Prism_ emits files modified and error messages,
plus a short summary.

```console
$ prism src/
error: cannot format src/prism_primer/cli.py: Cannot parse: 5:6: mport asyncio
reformatted src/prism_primer/lib.py
reformatted src/prismd/__init__.py
reformatted src/prism/__init__.py
Oh no! 💥 💔 💥
3 files reformatted, 2 files left unchanged, 1 file failed to reformat.
```

Passing `-v` / `--verbose` will cause _Prism_ to also emit messages about files that
were not changed or were ignored due to exclusion patterns. If _Prism_ is using a
configuration file, a blue message detailing which one it is using will be emitted.

```console
$ prism src/ -v
Using configuration from /tmp/pyproject.toml.
src/blib2to3 ignored: matches the --extend-exclude regular expression
src/_prism_version.py wasn't modified on disk since last run.
src/prism/__main__.py wasn't modified on disk since last run.
error: cannot format src/prism_primer/cli.py: Cannot parse: 5:6: mport asyncio
reformatted src/prism_primer/lib.py
reformatted src/prismd/__init__.py
reformatted src/prism/__init__.py
Oh no! 💥 💔 💥
3 files reformatted, 2 files left unchanged, 1 file failed to reformat
```

Passing `-q` / `--quiet` will cause _Prism_ to stop emitting all non-critial output.
Error messages will still be emitted (which can silenced by `2>/dev/null`).

```console
$ prism src/ -q
error: cannot format src/prism_primer/cli.py: Cannot parse: 5:6: mport asyncio
```

### Versions

You can check the version of _Prism_ you have installed using the `--version` flag.

```console
$ prism --version
prism, version 22.10.0
```

An option to require a specific version to be running is also provided.

```console
$ prism --required-version 21.9b0 -c "format = 'this'"
format = "this"
$ prism --required-version 31.5b2 -c "still = 'beta?!'"
Oh no! 💥 💔 💥 The required version does not match the running version!
```

This is useful for example when running _Prism_ in multiple environments that haven't
necessarily installed the correct version. This option can be set in a configuration
file for consistent results across environments.

## Configuration via a file

_Prism_ is able to read project-specific default values for its command line options
from a `pyproject.toml` file. This is especially useful for specifying custom
`--include` and `--exclude`/`--force-exclude`/`--extend-exclude` patterns for your
project.

**Pro-tip**: If you're asking yourself "Do I need to configure anything?" the answer is
"No". _Prism_ is all about sensible defaults. Applying those defaults will have your
code in compliance with many other _Prism_ formatted projects.

### What on Earth is a `pyproject.toml` file?

[PEP 518](https://www.python.org/dev/peps/pep-0518/) defines `pyproject.toml` as a
configuration file to store build system requirements for Python projects. With the help
of tools like [Poetry](https://python-poetry.org/),
[Flit](https://flit.readthedocs.io/en/latest/), or
[Hatch](https://hatch.pypa.io/latest/) it can fully replace the need for `setup.py` and
`setup.cfg` files.

### Where _Prism_ looks for the file

By default _Prism_ looks for `pyproject.toml` starting from the common base directory of
all files and directories passed on the command line. If it's not there, it looks in
parent directories. It stops looking when it finds the file, or a `.git` directory, or a
`.hg` directory, or the root of the file system, whichever comes first.

If you're formatting standard input, _Prism_ will look for configuration starting from
the current working directory.

You can use a "global" configuration, stored in a specific location in your home
directory. This will be used as a fallback configuration, that is, it will be used if
and only if _Prism_ doesn't find any configuration as mentioned above. Depending on your
operating system, this configuration file should be stored as:

- Windows: `~\.prism`
- Unix-like (Linux, MacOS, etc.): `$XDG_CONFIG_HOME/prism` (`~/.config/prism` if the
  `XDG_CONFIG_HOME` environment variable is not set)

Note that these are paths to the TOML file itself (meaning that they shouldn't be named
as `pyproject.toml`), not directories where you store the configuration. Here, `~`
refers to the path to your home directory. On Windows, this will be something like
`C:\\Users\UserName`.

You can also explicitly specify the path to a particular file that you want with
`--config`. In this situation _Prism_ will not look for any other file.

If you're running with `--verbose`, you will see a blue message if a file was found and
used.

Please note `prismd` will not use `pyproject.toml` configuration.

### Configuration format

As the file extension suggests, `pyproject.toml` is a
[TOML](https://github.com/toml-lang/toml) file. It contains separate sections for
different tools. _Prism_ is using the `[tool.prism]` section. The option keys are the
same as long names of options on the command line.

Note that you have to use single-quoted strings in TOML for regular expressions. It's
the equivalent of r-strings in Python. Multiline strings are treated as verbose regular
expressions by Prism. Use `[ ]` to denote a significant space character.

<details>
<summary>Example <code>pyproject.toml</code></summary>

```toml
[tool.prism]
line-length = 88
target-version = ['py37']
include = '\.pyi?$'
# 'extend-exclude' excludes files or directories in addition to the defaults
extend-exclude = '''
# A regex preceded with ^/ will apply only to files and directories
# in the root of the project.
(
  ^/foo.py    # exclude a file named foo.py in the root of the project
  | .*_pb2.py  # exclude autogenerated Protocol Buffer files anywhere in the project
)
'''
```

</details>

### Lookup hierarchy

Command-line options have defaults that you can see in `--help`. A `pyproject.toml` can
override those defaults. Finally, options provided by the user on the command line
override both.

_Prism_ will only ever use one `pyproject.toml` file during an entire run. It doesn't
look for multiple files, and doesn't compose configuration from different levels of the
file hierarchy.

## Next steps

You've probably noted that not all of the options you can pass to _Prism_ have been
covered. Don't worry, the rest will be covered in a later section.

A good next step would be configuring auto-discovery so `prism .` is all you need
instead of laborously listing every file or directory. You can get started by heading
over to [File collection and discovery](./file_collection_and_discovery.md).

Another good choice would be setting up an
[integration with your editor](../integrations/editors.md) of choice or with
[pre-commit for source version control](../integrations/source_version_control.md).
