"""Known invalid public API inputs use transport-independent library errors."""

from typing import Any

import pytest

from carbotek import Engine, render, render_pdf
from carbotek.errors import ValidationError


@pytest.mark.parametrize("template", [None, "template.docx", bytearray(b"zip"), 123])
@pytest.mark.parametrize("operation", ["compile", "render", "render_pdf", "module_render", "module_render_pdf"])
def test_invalid_template_type_has_library_error(template: Any, operation: str) -> None:
    engine = Engine()
    with pytest.raises(ValidationError, match="template must be bytes") as failure:
        if operation == "compile":
            engine.compile(template)
        elif operation == "render":
            engine.render(template, {})
        elif operation == "render_pdf":
            engine.render_pdf(template, {})
        elif operation == "module_render":
            render(template, {})
        else:
            render_pdf(template, {})
    assert failure.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("data", [None, 1, "text", ({},), [1], [None], [{}, "text"]])
@pytest.mark.parametrize("pdf", [False, True])
def test_invalid_data_type_has_library_error(data: Any, pdf: bool) -> None:
    engine = Engine()
    with pytest.raises(ValidationError, match="data must be a mapping or list of mappings"):
        if pdf:
            engine.render_pdf(b"invalid zip", data)
        else:
            engine.render(b"invalid zip", data)


@pytest.mark.parametrize("operation", ["compile", "render", "render_pdf"])
def test_non_string_option_key_has_library_error(operation: str) -> None:
    engine = Engine()
    options: Any = {1: "value"}
    with pytest.raises(ValidationError, match="option names must be strings"):
        if operation == "compile":
            engine.compile(b"invalid zip", options)
        elif operation == "render":
            engine.render(b"invalid zip", {}, options)
        else:
            engine.render_pdf(b"invalid zip", {}, options)


@pytest.mark.parametrize(
    "currency,message",
    [
        ({"rates": []}, "currency.rates must be a mapping"),
        ({"rates": None}, "currency.rates must be a mapping"),
        ({"rates": "bad"}, "currency.rates must be a mapping"),
        ({"rates": 1}, "currency.rates must be a mapping"),
        ({"rates": {1: 2}}, "currency.rates keys must be strings"),
        ({"source": []}, "currency.source must be a string or null"),
        ({"target": {}}, "currency.target must be a string or null"),
    ],
)
@pytest.mark.parametrize("direct", [False, True])
def test_nested_currency_validation_is_shared(currency: Any, message: str, direct: bool) -> None:
    from carbotek import RenderOptions

    with pytest.raises(ValidationError, match=message):
        if direct:
            RenderOptions(currency=currency)
        else:
            RenderOptions.create(currency=currency)


@pytest.mark.parametrize("direct", [False, True])
def test_currency_rates_are_copied_and_readonly(direct: bool) -> None:
    from carbotek import RenderOptions

    rates = {"USD": 1, "EUR": 2}
    options = RenderOptions(currency={"rates": rates}) if direct else RenderOptions.create(currency={"rates": rates})
    rates["EUR"] = 3
    assert options.currency["rates"]["EUR"] == 2
    with pytest.raises(TypeError):
        options.currency["rates"]["EUR"] = 4


@pytest.mark.parametrize("value", ["4", True, 0, -1, 1.5, None])
@pytest.mark.parametrize("name", ["max_bytes", "fetch_timeout_ms", "max_download_concurrency", "total_timeout_ms"])
def test_image_limits_require_positive_integers(name: str, value: Any) -> None:
    from carbotek.features.fetch import ImageSourceConfig

    with pytest.raises(ValidationError, match=name):
        ImageSourceConfig(**{name: value})


@pytest.mark.parametrize("allowlist", ["example.com", None, {"example.com"}, (1,), [None]])
def test_image_allowlist_has_typed_validation(allowlist: Any) -> None:
    from carbotek.features.fetch import ImageSourceConfig

    with pytest.raises(ValidationError, match="allowlist"):
        ImageSourceConfig(allowlist=allowlist)


def test_image_allowlist_copies_caller_list() -> None:
    from carbotek.features.fetch import ImageSourceConfig

    entries: Any = [" Example.COM. ", "", "images.example.com"]
    config = ImageSourceConfig(allowlist=entries)
    entries.append("untrusted.test")
    assert config.allowlist == ("example.com", "images.example.com")


@pytest.mark.parametrize("file_root", [None, 1, [], {}])
def test_image_file_root_has_typed_validation(file_root: Any) -> None:
    from carbotek.features.fetch import ImageSourceConfig

    with pytest.raises(ValidationError, match="file_root"):
        ImageSourceConfig(file_root=file_root)


@pytest.mark.parametrize("config", [{}, "root", 1, False])
def test_engine_image_sources_has_typed_validation(config: Any) -> None:
    with pytest.raises(ValidationError, match="image_sources"):
        Engine(image_sources=config)
