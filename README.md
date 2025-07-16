<h2 align="center">The Compromising Code Formatter</h2>

> “Any color you like.”

_Prism_ is the compromising Python code formatter. By using it, you agree to cede
control over some minutiae of hand-formatting, but can still actually configure things that matter. 
In return, _Prism_ gives you speed, determinism, and freedom. You will save time
and mental energy for more important matters.

---

## Installation and usage

### Installation

_Prism_ can be installed by running `pip install prism`. It requires Python 3.7+ to run.

If you can't wait for the latest _hotness_ and want to install from GitHub, use:

`pip install git+https://github.com/gsmethells/prism`

### Usage

To get started right away with sensible defaults:

```sh
prism {source_file_or_directory}
```

You can run _Prism_ as a package if running it as a script doesn't work:

```sh
python -m prism {source_file_or_directory}
```

## The _Prism_ code style

_Prism_ does not force PEP 8 on you (_whew_). 
Style configuration options are deliberately not limited. 

_Prism_ does reformats entire files in place. 
It does take previous formatting into account).

Our documentation covers the default _Prism_ code style, but feel free to tune it.

## Configuration

**Pro-tip**: If you're asking yourself "Do I need to configure anything?" the answer is
"No". _Prism_ is still all about sensible defaults. Applying those defaults will have your
code in compliance with many other _Prism_ formatted projects.

## License

MIT

## Contributing

Welcome! Happy to see you willing to make the project better. You can get started by
reading this:

- [Contributing: The basics](https://prism.readthedocs.io/en/latest/contributing/the_basics.html)

## Change log

The log has become rather long. It moved to its own file.

See [CHANGES](https://prism.readthedocs.io/en/latest/change_log.html).

## Authors

The author list is quite long nowadays, so it lives in its own file.

See [AUTHORS.md](./AUTHORS.md)

## Code of Conduct

Everyone participating in the _Prism_ project, and in particular in the issue tracker,
pull requests, and any other activity, is expected to treat other people with respect
and more generally to follow the guidelines articulated in the
[Python Community Code of Conduct](https://www.python.org/psf/codeofconduct/).

At the same time, humor is encouraged. In fact, basic familiarity with Monty Python's
Flying Circus is expected. We are not savages.

And if you _really_ need to slap somebody, do it with a fish while dancing.
