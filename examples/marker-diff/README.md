# Offline dependency marker diff explainer

`tasks/marker_diff.py` compares **one requirements list** against **two
explicit target environments** and explains which dependencies become
enabled/disabled because of environment markers -- fully offline (no PyPI,
no version resolution).

* The marker AST condition path (`and`/`or` + every comparison's resolved
  value) is preserved in the explanation.
* `extra` is evaluated per caller-provided extras set (`--extras fast,cli`,
  repeatable); it is never read from the environment.
* A marker variable missing from an environment is reported as `UNKNOWN`,
  never silently treated as `""`/false.
* Duplicate dependencies are aggregated by canonical name; conflicting
  specifiers keep their source lines and are reported as NOT installable.

## Demo

```console
$ python tasks/marker_diff.py examples/marker-diff/requirements-demo.txt \
    --env-a examples/marker-diff/env-py310.json \
    --env-b examples/marker-diff/env-py312.json \
    --name-a py310 --name-b py312 --extras fast --extras ""
```

shows, among others, `foo; python_version < "3.11"` becoming DISABLED with
the condition value changing `"3.10" -> "3.12"`, and the conflicting
`qux==1.4` / `qux>=2.0` declarations reported with their source lines.

## Tests

```console
$ python -m pytest tests/test_marker_diff.py -q
```
