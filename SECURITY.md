# Security policy

## Supported versions

The latest minor release receives fixes. While the project is pre-1.0, that means the newest `0.x` line only.

## How the tool can affect your machine

`deadweight` makes **no network calls at run time**. It reads your compilation database, your sources, and the linker reports and binaries you point it at, and it writes only the report files you ask for.

It executes exactly one class of external program: the compiler toolchain you already build with. The front end loads your libclang shared library through `ctypes` and asks it to parse your translation units; the cross-check tier invokes `nm`, `readelf` and `c++filt` on the files you supply. It never downloads a compiler, never installs anything, and never runs your tests or your build system. Nothing in your tree is executed as a program.

The one thing worth knowing: parsing a translation unit hands your compiler a file you control, and a compiler can be crashed by pathological input. That is why a translation unit is parsed in its own spawned worker process; a crash costs that unit and makes the run exit `2` rather than shrinking the graph silently.

## Reporting a vulnerability

Please report privately, not in a public issue: open a private advisory through the repository's **Security** tab ("Report a vulnerability"). That channel notifies the maintainers without disclosing the problem.

Include the version (`deadweight --version`), the output of `deadweight doctor`, and the smallest input that reproduces the problem. You can expect an acknowledgement within a week, and a fix or an explanation of why the report is not a vulnerability as soon as the cause is clear. Please give us the chance to release a fix before you publish.
