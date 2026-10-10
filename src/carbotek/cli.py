# pyright: strict, reportPrivateUsage=false
"""Command line interface.

    carbotek render TEMPLATE DATA [-o OUT] [--pdf] [--lang ru] [--timezone Europe/Moscow] [--options JSON]
    carbotek bench  TEMPLATE DATA [-n 20] [--warmup 1] [--pdf] [--no-cache] [--profile]
    carbotek markers TEMPLATE

``DATA`` is a JSON file (``-`` reads stdin). ``--options`` takes :meth:`RenderOptions.create`
keywords as JSON (``{"complement": {...}, "currency": {"source": "EUR"}, ...}``).
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .api import Engine
from .cache import clear_module_caches
from .errors import CarbotekError, ValidationError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except CarbotekError as error:
        sys.stderr.write(f"{error.code}: {error}\n")
        return 2
    except (OSError, json.JSONDecodeError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carbotek", description="Render DOCX and XLSX templates with Carbotek markers."
    )
    parser.add_argument("--version", action="version", version=f"carbotek {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render", help="render one document")
    _input_arguments(render)
    render.add_argument("-o", "--output", type=Path, help="output file (default: TEMPLATE stem + .out.docx/.xlsx/.pdf)")
    render.set_defaults(handler=_render)

    bench = commands.add_parser("bench", help="measure repeated renders of one template")
    _input_arguments(bench)
    bench.add_argument("-n", "--iterations", type=int, default=20, help="measured renders (default 20)")
    bench.add_argument("--warmup", type=int, default=1, help="unmeasured renders first (default 1)")
    bench.add_argument(
        "--no-cache", action="store_true", help="clear module caches and create a new engine for every render"
    )
    bench.add_argument("--profile", action="store_true", help="print the cProfile top 25 of the measured renders")
    bench.add_argument("--json", action="store_true", help="print the result as JSON")
    bench.set_defaults(handler=_bench)

    markers = commands.add_parser("markers", help="list the markers of a template after normalization")
    markers.add_argument("template", type=Path)
    markers.set_defaults(handler=_markers)
    return parser


def _input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("template", type=Path, help="DOCX or XLSX template")
    parser.add_argument("data", help="JSON data file, or - for stdin")
    parser.add_argument("--pdf", action="store_true", help="convert the result to PDF (dxpdf)")
    parser.add_argument("--image-dpi", type=int, default=220, help="PDF image DPI (default 220)")
    parser.add_argument("--lang", help="document language (default ru)")
    parser.add_argument("--timezone", help="IANA time zone (default Europe/Moscow)")
    parser.add_argument("--options", default="{}", help="RenderOptions.create keywords as JSON")


def _load(arguments: argparse.Namespace) -> tuple[bytes, Any, dict[str, Any]]:
    template = Path(arguments.template).read_bytes()
    raw = sys.stdin.read() if arguments.data == "-" else Path(arguments.data).read_text(encoding="utf-8")
    options: dict[str, Any] = json.loads(arguments.options)
    if not isinstance(options, dict):  # pyright: ignore[reportUnnecessaryIsInstance] - JSON may be an array
        raise ValidationError("--options must contain a JSON object")
    if arguments.lang:
        options["lang"] = arguments.lang
    if arguments.timezone:
        options["timezone"] = arguments.timezone
    return template, json.loads(raw), options


def _renderer(engine: Engine, arguments: argparse.Namespace) -> Callable[[bytes, Any, dict[str, Any]], bytes]:
    if arguments.pdf:
        return lambda template, data, options: engine.render_pdf(template, data, options, image_dpi=arguments.image_dpi)
    return engine.render


def _render(arguments: argparse.Namespace) -> int:
    template, data, options = _load(arguments)
    started = time.perf_counter()
    engine = Engine()
    compiled = engine.compile(template, options)
    result = _renderer(engine, arguments)(template, data, options)
    elapsed = time.perf_counter() - started
    suffix = "." + compiled.package.kind
    output: Path = arguments.output or Path(arguments.template).with_suffix(
        ".out.pdf" if arguments.pdf else ".out" + suffix
    )
    output.write_bytes(result)
    sys.stderr.write(f"{output} ({len(result)} bytes, {elapsed * 1000:.1f} ms)\n")
    return 0


def _bench(arguments: argparse.Namespace) -> int:
    template, data, options = _load(arguments)
    shared = Engine()

    def once() -> float:
        if arguments.no_cache:
            clear_module_caches()
        started = time.perf_counter()
        engine = Engine(cache_size=0) if arguments.no_cache else shared
        _renderer(engine, arguments)(template, data, options)
        return time.perf_counter() - started

    cold = once()
    for _ in range(max(arguments.warmup - 1, 0)):
        once()
    profiler = None
    if arguments.profile:
        import cProfile

        profiler = cProfile.Profile()
        profiler.enable()
    timings = [once() for _ in range(max(arguments.iterations, 1))]
    if profiler is not None:
        profiler.disable()
    result = _summary(cold, timings, None if arguments.no_cache else shared.metrics)
    if arguments.json:
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        # Read the package kind after timing; metadata must not warm a measured cold run.
        kind = "pdf" if arguments.pdf else Engine(cache_size=0).compile(template, options).package.kind
        sys.stdout.write(
            f"{arguments.template.name}: {kind}, n={result['iterations']}"
            f"{', no cache' if arguments.no_cache else ''}\n"
            f"  cold {result['cold_ms']:.1f} ms | mean {result['mean_ms']:.1f} | p50 {result['p50_ms']:.1f}"
            f" | p95 {result['p95_ms']:.1f} | min {result['min_ms']:.1f} | max {result['max_ms']:.1f} ms"
            f" | {result['per_second']:.1f}/s\n"
        )
        if result.get("cache"):
            sys.stdout.write(f"  compile cache: {result['cache']}\n")
    if profiler is not None:
        import pstats

        pstats.Stats(profiler, stream=sys.stderr).sort_stats("cumulative").print_stats(25)
    return 0


def _summary(cold: float, timings: list[float], cache: dict[str, int | float] | None) -> dict[str, Any]:
    ordered = sorted(timings)
    mean = statistics.fmean(ordered)
    return {
        "iterations": len(ordered),
        "cold_ms": cold * 1000,
        "mean_ms": mean * 1000,
        "p50_ms": statistics.median(ordered) * 1000,
        "p95_ms": ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))] * 1000,
        "min_ms": ordered[0] * 1000,
        "max_ms": ordered[-1] * 1000,
        "per_second": 1 / mean if mean else 0.0,
        "cache": cache,
    }


def _markers(arguments: argparse.Namespace) -> int:
    from .markers import scan
    from .ooxml.xml import parse_xml

    compiled = Engine().compile(arguments.template.read_bytes())
    package = compiled.package
    for name in package.order:
        part = package.parts[name]
        if not part.is_xml:
            continue
        for node in parse_xml(part.content).iter():
            texts = [*node.attrib.values(), node.text or ""] if isinstance(node.tag, str) else []
            for text in texts:
                for occurrence in scan(str(text), include_aliases=True):
                    sys.stdout.write(f"{name}\t{occurrence.raw}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
