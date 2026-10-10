"""Template origins and one document-wide evaluation context."""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree

from carbotek import Engine, RenderOptions
from carbotek.docx import loops as loop_module
from carbotek.docx import renderer as renderer_module
from carbotek.docx.renderer import DocxTextRenderer
from carbotek.docx.session import CURRENT_RENDER
from carbotek.errors import FormatterError, InvalidTemplateError, ValidationError
from carbotek.formatters import default_registry
from carbotek.formatters.registry import UnknownFormatterError
from carbotek.markers import parse
from carbotek.ooxml.ns import NAMESPACES, qn

_NAMESPACES = " ".join(f'xmlns:{key}="{value}"' for key, value in NAMESPACES.items())


def paragraph(text: str, *, properties: str = "") -> str:
    return f"<w:p><w:r>{properties}<w:t>{escape(text)}</w:t></w:r></w:p>"


def document(body: str, **parts: str) -> bytes:
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    files = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="word/document.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            "</Relationships>"
        ),
        "word/document.xml": f"<w:document {_NAMESPACES}><w:body>{body}<w:sectPr/></w:body></w:document>",
        **parts,
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, source in files.items():
            archive.writestr(name, source)
    return stream.getvalue()


def root(output: bytes, name: str = "word/document.xml") -> etree._Element:
    with ZipFile(BytesIO(output)) as archive:
        return etree.fromstring(archive.read(name))


def text(output: bytes, name: str = "word/document.xml") -> str:
    return "".join(root(output, name).itertext())


@pytest.fixture(params=[False, True], ids=["native", "python"])
def engine(request: pytest.FixtureRequest) -> Engine:
    if request.param:
        request.getfixturevalue("python_only")
    return Engine()


@pytest.mark.parametrize(
    "action,condition,hidden",
    [("drop", True, True), ("drop", False, False), ("keep", False, True), ("keep", True, False)],
)
@pytest.mark.parametrize("count", [2, 3])
def test_multi_paragraph_directive_counts_template_before_loop(
    engine: Engine, action: str, condition: bool, hidden: bool, count: int
) -> None:
    template = document(
        paragraph(f"ANCHOR{{d.remove:{action}(p,{count})}}")
        + paragraph("{d.rows[i].x}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(template, {"remove": condition, "rows": [{"x": "A"}, {"x": "B"}, {"x": "C"}]})
    assert text(output) == ("AFTER" if hidden else "ANCHORABCAFTER")
    assert not root(output).findall(".//carbotek-directive-boundary")


def test_multi_paragraph_directive_does_not_consume_following_paragraph_on_empty_loop(engine: Engine) -> None:
    template = document(
        paragraph("ANCHOR{d.remove:drop(p,3)}")
        + paragraph("{d.rows[i].x}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(template, {"remove": True, "rows": []})
    assert text(output) == "AFTER"


def test_multi_paragraph_directive_inside_loop_uses_each_copy_scope(engine: Engine) -> None:
    template = document(
        paragraph("{d.rows[i].x}{d.rows[i].remove:drop(p,2)}")
        + paragraph("DETAIL")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(template, {"rows": [{"x": "A", "remove": True}, {"x": "B", "remove": False}]})
    assert text(output) == "BDETAILAFTER"
    assert b"carbotek-directive-boundary" not in output_part(output)


@pytest.mark.parametrize("first", [False, True])
@pytest.mark.parametrize("second", [False, True])
def test_multi_paragraph_directive_spanning_loop_end_restores_condition_after_loop(
    engine: Engine, first: bool, second: bool
) -> None:
    template = document(
        paragraph("{d.rows[i].x}{d.rows[i].remove:drop(p,2)}") + paragraph("{d.rows[i+1].x}") + paragraph("AFTER")
    )
    output = engine.render(template, {"rows": [{"x": "A", "remove": first}, {"x": "B", "remove": second}]})
    assert text(output) == ("AFTER" if first else "ABAFTER")


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("outer_hidden", [None, False, True])
def test_directive_spanning_empty_loop_preserves_surrounding_block(
    engine: Engine, action: str, outer_hidden: bool | None
) -> None:
    body = (
        paragraph(f"{{d.rows[i].x}}{{d.rows[i].remove:{action}(p,2)}}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    if outer_hidden is not None:
        body = paragraph("{d.hide:hideBegin}") + body + paragraph("{d.hide:hideEnd}") + paragraph("OUTSIDE")
    output = engine.render(document(body), {"rows": [], "hide": outer_hidden})
    expected = "AFTER" if outer_hidden is None else "OUTSIDE" if outer_hidden else "AFTEROUTSIDE"
    assert text(output) == expected
    assert b"carbotek-directive-boundary" not in output_part(output)


def output_part(output: bytes) -> bytes:
    with ZipFile(BytesIO(output)) as archive:
        return archive.read("word/document.xml")


def test_variable_declared_in_later_header_is_available_in_main_and_footnotes(engine: Engine) -> None:
    template = document(
        paragraph("{$label}"),
        **{
            "word/header1.xml": f"<w:hdr {_NAMESPACES}>" + paragraph("{#label=d.name}") + "</w:hdr>",
            "word/footnotes.xml": f'<w:footnotes {_NAMESPACES}><w:footnote w:id="1">'
            + paragraph("{$label}")
            + "</w:footnote></w:footnotes>",
        },
    )
    output = engine.render(template, {"name": "VALUE"})
    assert text(output) == "VALUE"
    assert text(output, "word/footnotes.xml") == "VALUE"
    assert "#label" not in text(output, "word/header1.xml")


def test_cross_part_alias_enables_html_feature(engine: Engine) -> None:
    template = document(
        paragraph("{$html}"),
        **{
            "word/header1.xml": f"<w:hdr {_NAMESPACES}>" + paragraph("{#html=d.content:html}") + "</w:hdr>",
        },
    )
    output = engine.render(template, {"content": "<b>VALUE</b>"})
    assert text(output) == "VALUE"
    assert root(output).find(".//w:b", NAMESPACES) is not None


@pytest.mark.parametrize("source", ["template", "options"])
def test_set_hidden_in_alias_executes_before_parts_and_preserves_caller(engine: Engine, source: str) -> None:
    declaration = "{#assign=d.rows[].v:set(d.target)}"
    parts = (
        {"word/header1.xml": f"<w:hdr {_NAMESPACES}>" + paragraph(declaration) + "</w:hdr>"}
        if source == "template"
        else {}
    )
    template = document(paragraph("{d.target}") + paragraph("{$assign}"), **parts)
    data = {"rows": [{"v": 2}, {"v": 3}]}
    options = {"variable_str": declaration} if source == "options" else None
    output = engine.render(template, data, options)
    assert text(output) == "3"
    assert data == {"rows": [{"v": 2}, {"v": 3}]}


def test_compile_cache_distinguishes_option_variable_definitions(engine: Engine) -> None:
    template = document(paragraph("{$label}"))
    data = {"a": "A", "b": "B"}
    assert text(engine.render(template, data, {"variable_str": "{#label=d.a}"})) == "A"
    assert text(engine.render(template, data, {"variable_str": "{#label=d.b}"})) == "B"
    assert engine.metrics["misses"] == 2


def test_chart_alias_survives_rendering_other_parts(engine: Engine) -> None:
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    template = document(
        f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>',
        **{
            "word/header1.xml": f"<w:hdr {_NAMESPACES}>" + paragraph("{#label=d.name}") + "</w:hdr>",
            "word/footnotes.xml": f'<w:footnotes {_NAMESPACES}><w:footnote w:id="1">'
            + paragraph("FOOTNOTE")
            + "</w:footnote></w:footnotes>",
            "word/charts/chart1.xml": f'<c:chartSpace xmlns:c="{chart_ns}" xmlns:a="{NAMESPACES["a"]}"><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>{{$label}}</a:t></a:r></a:p></c:rich></c:tx></c:title></c:chart></c:chartSpace>',
            "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + f'<Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{NAMESPACES["r"]}/chart"/></Relationships>',
        },
    )
    output = engine.render(template, {"name": "VALUE"})
    with ZipFile(BytesIO(output)) as archive:
        rendered = etree.fromstring(archive.read("word/charts/CarbotekChart0.xml"))
    assert "".join(rendered.itertext()) == "VALUE"


def test_cli_output_extension_comes_from_package_content(tmp_path, capsys) -> None:
    from carbotek.cli import main

    template = tmp_path / "misnamed.xlsx"
    template.write_bytes(document(paragraph("{d.name}")))
    data = tmp_path / "data.json"
    data.write_text('{"name":"VALUE"}', encoding="utf-8")
    assert main(["render", str(template), str(data)]) == 0
    output = tmp_path / "misnamed.out.docx"
    assert output.is_file()
    assert text(output.read_bytes()) == "VALUE"
    assert not (tmp_path / "misnamed.out.xlsx").exists()


def test_cli_markers_validates_zip_and_uses_compiled_normalization(tmp_path, capsys) -> None:
    from carbotek.cli import main

    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a package")
    assert main(["markers", str(bad)]) == 2
    assert "TEMPLATE_ERROR" in capsys.readouterr().err
    template = tmp_path / "template.docx"
    template.write_bytes(document("<w:p><w:r><w:t>{d.na</w:t></w:r><w:r><w:t>me}</w:t></w:r></w:p>"))
    assert main(["markers", str(template)]) == 0
    assert "word/document.xml\t{d.name}" in capsys.readouterr().out


def test_cli_options_require_json_object(tmp_path, capsys) -> None:
    from carbotek.cli import main

    template = tmp_path / "template.docx"
    template.write_bytes(document(paragraph("{d.name}")))
    data = tmp_path / "data.json"
    data.write_text("{}", encoding="utf-8")
    assert main(["render", str(template), str(data), "--options", "[]"]) == 2
    assert "VALIDATION_ERROR" in capsys.readouterr().err


def test_cli_no_cache_runs_have_independent_registry_and_parser_misses(tmp_path, capsys, monkeypatch) -> None:
    from carbotek.cli import main
    from carbotek.formatters import shared_registry
    from carbotek.markers import parse

    template = tmp_path / "template.docx"
    template.write_bytes(document(paragraph("{d.name}")))
    data = tmp_path / "data.json"
    data.write_text('{"name":"VALUE"}', encoding="utf-8")
    original = Engine.render
    samples = []

    def record(self, template, data, options=None):
        output = original(self, template, data, options)
        samples.append((shared_registry(), parse.cache_info().misses))
        return output

    monkeypatch.setattr(Engine, "render", record)
    assert main(["bench", str(template), str(data), "--no-cache", "-n", "2", "--json"]) == 0
    assert len(samples) == 3
    assert len({id(sample[0]) for sample in samples}) == 3
    assert len({sample[1] for sample in samples}) == 1


def test_missing_formatter_arguments_have_js_semantics_on_all_paths(engine: Engine) -> None:
    output = engine.render(
        document(paragraph("{d.x:padl}/{d.x:padr}") + paragraph("{d.rows[i].x:padl}/{d.rows[i+1].x}")),
        {"x": "X", "rows": [{"x": "Y"}]},
    )
    assert text(output) == "X/XY/"


@pytest.mark.parametrize(
    "value,expected", [("0o17", "15"), ("0b11", "3"), ("1_000", "NaN"), ("inf", "NaN"), ("١", "NaN"), ("", "0")]
)
def test_currency_conversion_uses_shared_js_number_rules(engine: Engine, value: str, expected: str) -> None:
    output = engine.render(
        document(paragraph("{d.x:convCurr}")),
        {"x": value},
        {"currency": {"source": "RUB", "target": "RUB"}},
    )
    assert text(output) == expected


def test_empty_currency_value_does_not_raise_decimal_error(engine: Engine) -> None:
    output = engine.render(
        document(paragraph("{d.x:formatC}")),
        {"x": ""},
        {"lang": "en", "currency": {"source": "USD", "target": "USD"}},
    )
    assert text(output) == "0.00 $"


@pytest.mark.parametrize(
    "mime,format_name,extension",
    [
        ("image/webp", "WEBP", "webp"),
        ("image/x-png", "PNG", "png"),
        ("image/pjpeg", "JPEG", "jpeg"),
        ("image/x-emf", "EMF", "emf"),
    ],
)
def test_docx_image_mime_variants_preserve_media(engine: Engine, mime: str, format_name: str, extension: str) -> None:
    from PIL import Image

    from carbotek.xlsx.images import _DEFAULT_EMF

    if format_name == "EMF":
        payload = _DEFAULT_EMF
    else:
        stream = BytesIO()
        Image.new("RGB", (12, 6), "red").save(stream, format=format_name)
        payload = stream.getvalue()
    source = f"data:{mime};base64," + b64encode(payload).decode("ascii")
    picture = (
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="100" cy="100"/>'
        '<wp:docPr id="1" name="Picture" descr="{d.picture}"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdTemplate"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    output = engine.render(document(picture), {"picture": source})
    with ZipFile(BytesIO(output)) as archive:
        names = [name for name in archive.namelist() if name.startswith("word/media/CarbotekImage")]
        assert names == [f"word/media/CarbotekImage0.{extension}"]
        assert archive.read(names[0]) == payload
        types = etree.fromstring(archive.read("[Content_Types].xml"))
    assert any(node.get("Extension") == extension for node in types)
    extent = root(output).find(".//wp:extent", NAMESPACES)
    assert extent is not None
    if format_name == "WEBP":
        assert int(extent.get("cx")) == int(extent.get("cy")) * 2


@pytest.mark.parametrize("remote", [False, True])
def test_docpr_without_blip_does_not_create_orphan_media(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    from PIL import Image

    from carbotek.features import image

    def unexpected_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("A docPr without a blip must not fetch")

    monkeypatch.setattr(image, "fetch_image", unexpected_fetch)
    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    source = "data:image/png;base64," + b64encode(stream.getvalue()).decode("ascii")
    if remote:
        source = "https://example.com/picture.png"
    body = '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="" descr="{d.picture}"/></wp:inline></w:drawing></w:r></w:p>'
    output = engine.render(document(body), {"picture": source})
    with ZipFile(BytesIO(output)) as archive:
        assert not any(name.startswith("word/media/") for name in archive.namelist())
        assert "word/_rels/document.xml.rels" not in archive.namelist()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://user:pass@example.com/a", "https://user:pass@example.com/a"),
        ("https://example.com:0/a", "https://example.com:0/a"),
        ("https://example.com:99999/a", None),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/%20/a b", "https://example.com/%20/a b"),
        ("mailto:a@example.com", "https://mailto:a@example.com"),
        ("https://exa mple.com", None),
        (" https://example.com/a", None),
    ],
)
def test_html_and_native_links_share_js_url_validation(engine: Engine, url: str, expected: str | None) -> None:
    from carbotek.formatters.docx import URL_ON_ERROR

    body = (
        paragraph("{d.html:html}") + '<w:p><w:hyperlink r:id="{d.url}"><w:r><w:t>NATIVE</w:t></w:r></w:hyperlink></w:p>'
    )
    template = document(
        body,
        **{
            "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        },
    )
    output = engine.render(template, {"html": f'<a href="{url}">HTML</a>', "url": url})
    with ZipFile(BytesIO(output)) as archive:
        relationships = etree.fromstring(archive.read("word/_rels/document.xml.rels"))
    targets = {node.get("Target") for node in relationships if node.get("Type", "").endswith("/hyperlink")}
    assert targets == {expected or URL_ON_ERROR}
    native = next(node for node in root(output).iter(qn("w:hyperlink")) if "NATIVE" in "".join(node.itertext()))
    assert (native.get(qn("r:id")) is not None) == (expected is not None)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/%20/\x01", "https://example.com/%20/%01"),
        ("https://example.com/%20/\ud800", None),
        ("https://[invalid]/a", None),
    ],
)
def test_native_url_invalid_xml_and_host_syntax_never_escape_error_contract(
    engine: Engine, url: str, expected: str | None
) -> None:
    template = document(
        '<w:p><w:hyperlink r:id="{d.url}"><w:r><w:t>LINK</w:t></w:r></w:hyperlink></w:p>',
        **{
            "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        },
    )
    output = engine.render(template, {"url": url})
    with ZipFile(BytesIO(output)) as archive:
        relationships = etree.fromstring(archive.read("word/_rels/document.xml.rels"))
    targets = [node.get("Target") for node in relationships]
    assert targets == ([] if expected is None else [expected])


@pytest.mark.parametrize("root", ["d", "c"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("selector", ["[i]", "[0]", "[name='B']", "[9]", "[name='Z']"])
def test_native_chart_loop_uses_its_copy_scope(engine: Engine, empty: bool, selector: str, root: str) -> None:
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    rel_ns = NAMESPACES["r"]
    chart = (
        f'<c:chartSpace xmlns:c="{chart_ns}"><c:chart><c:plotArea><c:barChart><c:ser>'
        '<c:val><c:numRef><c:f>Sheet1!$A$1:$A$2</c:f><c:numCache><c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>{d.groups[i].values[i].n}</c:v></c:pt>'
        '<c:pt idx="1"><c:v>{d.groups[i].values[i+1].n}</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )
    chart = chart.replace("groups[i]", "groups" + selector).replace("{d.", "{" + root + ".")
    body = (
        paragraph("{d.groups[i].name}")
        + (f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>')
        + paragraph("{d.groups[i+1].name}")
    ).replace("{d.", "{" + root + ".")
    template = document(
        body,
        **{
            "word/charts/chart1.xml": chart,
            "word/_rels/document.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{rel_ns}/chart"/></Relationships>'
            ),
        },
    )
    data = {
        "groups": [
            {"name": "A", "values": [{"n": 11}, {"n": 12}]},
            {"name": "B", "values": [{"n": 21}]},
        ]
    }
    if empty:
        data["groups"] = []
    output = engine.render(
        template,
        data if root == "d" else {"groups": [{"name": "DECOY", "values": [{"n": 999}]}]},
        {} if root == "d" else {"complement": data},
    )
    with ZipFile(BytesIO(output)) as archive:
        assert "word/charts/chart1.xml" not in archive.namelist()
        assert b"rIdChart" not in archive.read("word/_rels/document.xml.rels")
        rendered = [
            etree.fromstring(archive.read(name))
            for name in archive.namelist()
            if "CarbotekChart" in name and name.endswith(".xml")
        ]
    expected = {
        "[i]": [["11", "12"], ["21"]],
        "[0]": [["11", "12"], ["11", "12"]],
        "[name='B']": [["21"], ["21"]],
        "[9]": [[], []],
        "[name='Z']": [[], []],
    }
    assert [[node.text for node in item.iter(f"{{{chart_ns}}}v")] for item in rendered] == (
        [] if empty else expected[selector]
    )
    for item in rendered:
        cache = item.find(f".//{{{chart_ns}}}numCache")
        assert cache is not None
        points = cache.findall(f"{{{chart_ns}}}pt")
        count = cache.find(f"{{{chart_ns}}}ptCount")
        assert count is not None and count.get("val") == str(len(points))
        assert [point.get("idx") for point in points] == [str(index) for index in range(len(points))]


@pytest.mark.parametrize("alias_source", ["header", "options"])
def test_chart_list_aliases_preserve_literal_cache_values(engine: Engine, alias_source: str) -> None:
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    rel_ns = NAMESPACES["r"]
    declaration = "{#labels=d.groups[i].values[i].label}{#labelsEnd=d.groups[i].values[i+1].label}"
    chart = (
        f'<c:chartSpace xmlns:c="{chart_ns}"><c:chart><c:plotArea><c:barChart><c:ser>'
        '<c:cat><c:strRef><c:f>Sheet1!$A$1:$A$2</c:f><c:strCache><c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>{$labels}</c:v></c:pt><c:pt idx="1"><c:v>{$labelsEnd}</c:v></c:pt>'
        "</c:strCache></c:strRef></c:cat></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
    )
    parts = {
        "word/charts/chart1.xml": chart,
        "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{rel_ns}/chart"/></Relationships>',
    }
    if alias_source == "header":
        parts["word/header1.xml"] = f"<w:hdr {_NAMESPACES}>" + paragraph(declaration) + "</w:hdr>"
    body = (
        paragraph("{d.groups[i].name}")
        + f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>'
        + paragraph("{d.groups[i+1].name}")
    )
    options = {"variable_str": declaration, "complement": {"secret": "COMPLEMENT_LEAK"}}
    if alias_source == "header":
        options.pop("variable_str")
    output = engine.render(
        document(body, **parts),
        {
            "secret": "DATA_LEAK",
            "groups": [
                {"name": "A", "values": [{"label": "{d.secret}"}, {"label": "{c.secret}"}]},
                {"name": "B", "values": [{"label": "<& {$labels}:html"}]},
            ],
        },
        options,
    )
    with ZipFile(BytesIO(output)) as archive:
        charts = [
            etree.fromstring(archive.read(name))
            for name in sorted(archive.namelist())
            if "CarbotekChart" in name and name.endswith(".xml")
        ]
        assert [[node.text for node in item.iter(f"{{{chart_ns}}}v")] for item in charts] == [
            ["{d.secret}", "{c.secret}"],
            ["<& {$labels}:html"],
        ]
        assert [[node.get("val") for node in item.iter(f"{{{chart_ns}}}ptCount")] for item in charts] == [["2"], ["1"]]
        assert "word/charts/chart1.xml" not in archive.namelist()
        assert all(
            b"DATA_LEAK" not in archive.read(name) and b"COMPLEMENT_LEAK" not in archive.read(name)
            for name in archive.namelist()
            if name.endswith(".xml")
        )


@pytest.mark.parametrize("remove", [True, False])
def test_multi_row_directive_counts_template_rows(engine: Engine, remove: bool) -> None:
    def row(value: str) -> str:
        return "<w:tr><w:tc>" + paragraph(value) + "</w:tc></w:tr>"

    template = document(
        "<w:tbl>"
        + row("ANCHOR{d.remove:drop(row,3)}")
        + row("{d.rows[i].x}")
        + row("{d.rows[i+1].x}")
        + row("AFTER")
        + "</w:tbl>"
    )
    output = engine.render(template, {"remove": remove, "rows": [{"x": "A"}, {"x": "B"}, {"x": "C"}]})
    assert text(output) == ("AFTER" if remove else "ANCHORABCAFTER")
    assert len(root(output).findall(".//w:tr", NAMESPACES)) == (1 if remove else 5)
    assert b"carbotek-directive-boundary" not in output_part(output)


def test_chart_titles_use_session_aliases_complement_and_literal_values(engine: Engine) -> None:
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    chart = (
        f'<c:chartSpace xmlns:c="{chart_ns}" xmlns:a="{NAMESPACES["a"]}"><c:chart><c:title>'
        "<c:tx><c:rich><a:p><a:r><a:t>{$label}/{c.suffix}</a:t></a:r></a:p></c:rich></c:tx>"
        "</c:title></c:chart></c:chartSpace>"
    )
    body = (
        paragraph("{d.groups[i].name}")
        + f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>'
        + paragraph("{d.groups[i+1].name}")
    )
    template = document(
        body,
        **{
            "word/charts/chart1.xml": chart,
            "word/_rels/document.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{NAMESPACES["r"]}/chart"/></Relationships>'
            ),
        },
    )
    output = engine.render(
        template,
        {"groups": [{"name": "A"}, {"name": "{d.secret}"}], "secret": "LEAK"},
        {"variable_str": "{#label=d.groups[i].name}", "complement": {"suffix": "SUFFIX"}},
    )
    with ZipFile(BytesIO(output)) as archive:
        names = [name for name in archive.namelist() if "CarbotekChart" in name and name.endswith(".xml")]
        titles = ["".join(etree.fromstring(archive.read(name)).itertext()) for name in names]
    assert titles == ["A/SUFFIX", "{d.secret}/SUFFIX"]


def test_multi_paragraph_directive_preserves_quoted_formatter_arguments(engine: Engine) -> None:
    output = engine.render(
        document(paragraph("{d.value:ifEQ('A:B'):drop(p,2)}") + paragraph("HIDDEN") + paragraph("AFTER")),
        {"value": "A:B"},
    )
    assert text(output) == "AFTER"


def test_multi_paragraph_directive_in_attribute_is_compiled_before_expansion(engine: Engine) -> None:
    first = '<w:p custom="{d.remove:drop(p,3)}"><w:r><w:t>ANCHOR</w:t></w:r></w:p>'
    template = document(first + paragraph("{d.rows[i].x}") + paragraph("{d.rows[i+1].x}") + paragraph("AFTER"))
    output = engine.render(template, {"remove": True, "rows": [{"x": "A"}, {"x": "B"}]})
    assert text(output) == "AFTER"


@pytest.mark.parametrize("injected", ["{d.secret}", "{c.apiKey}", "{d.x:unknownFmt}", "{d.x:showBegin}"])
@pytest.mark.parametrize("col_text", ["", " (collectively)"])
def test_inline_loop_values_are_literal(engine: Engine, injected: str, col_text: str) -> None:
    template = document(paragraph("{d.rows[i].x}/{d.rows[i+1].x}" + col_text))
    output = engine.render(template, {"rows": [{"x": injected}], "secret": "LEAK"}, {"complement": {"apiKey": "KEY"}})
    assert text(output) == injected + "/" + col_text
    assert CURRENT_RENDER.get() is None


def test_column_directive_does_not_evaluate_rendered_data(engine: Engine) -> None:
    body = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + paragraph("{d.rows[i].x}")
        + "</w:tc><w:tc>"
        + paragraph("{d.remove:drop(col)}")
        + "</w:tc></w:tr>"
        "<w:tr><w:tc>" + paragraph("{d.rows[i+1].x}") + "</w:tc></w:tr></w:tbl>"
    )
    output = engine.render(document(body), {"rows": [{"x": "{d.secret}"}], "secret": "LEAK", "remove": False})
    assert "{d.secret}" in text(output)
    assert "LEAK" not in text(output)


@pytest.mark.parametrize("injected", ["{d.secret:html(inline)}", "{d.secret:color(p)}"])
def test_features_do_not_scan_values(engine: Engine, injected: str) -> None:
    template = document(paragraph("{d.literal}") + paragraph("{d.h:html}") + paragraph("{d.c:color(p)}"))
    output = engine.render(template, {"literal": injected, "h": "<p>real</p>", "c": "000000", "secret": "LEAK"})
    assert injected in text(output)
    assert "real" in text(output)
    assert "LEAK" not in text(output)


def test_html_output_does_not_create_markers(engine: Engine) -> None:
    output = engine.render(
        document(paragraph("{d.h:html}")), {"h": "<p>{c.apiKey:html(inline)}</p>"}, {"complement": {"apiKey": "LEAK"}}
    )
    assert text(output) == "{c.apiKey:html(inline)}"


def test_bind_color_is_compiled_into_each_loop_copy(engine: Engine) -> None:
    body = paragraph("{bindColor(123456,hexa)=d.rows[i].c}")
    body += paragraph("{d.rows[i].x}", properties='<w:rPr><w:color w:val="123456"/></w:rPr>')
    body += paragraph("{d.rows[i+1].x}")
    output = engine.render(document(body), {"rows": [{"x": "A", "c": "ff0000"}, {"x": "B", "c": "00ff00"}]})
    assert [node.get(qn("w:val")) for node in root(output).iter(qn("w:color"))] == ["ff0000", "00ff00"]


def test_set_is_visible_to_header_and_html(engine: Engine) -> None:
    template = document(
        paragraph("{d.name:prepend('<b>'):append('</b>'):set(d.h)}") + paragraph("{d.h:html}"),
        **{"word/header1.xml": f"<w:hdr {_NAMESPACES}>{paragraph('{d.h}')}</w:hdr>"},
    )
    data = {"name": "Ann"}
    output = engine.render(template, data)
    assert text(output) == "Ann"
    assert text(output, "word/header1.xml") == "<b>Ann</b>"
    assert data == {"name": "Ann"}


def test_set_uses_condition_pipeline(engine: Engine) -> None:
    output = engine.render(document(paragraph("{d.x:ifEQ('paid'):show('Yes'):set(c.label)}{c.label}")), {"x": "paid"})
    assert text(output) == "Yes"


def test_html_missing_is_undefined(engine: Engine) -> None:
    output = engine.render(document(paragraph("{d.m:ifEQ(null):show('A'):elseShow('B'):html}")), {})
    assert text(output) == "B"


def test_color_uses_condition_result(engine: Engine) -> None:
    output = engine.render(document(paragraph("{d.c:ifEQ('000000'):show('FF0000'):color(p)}")), {"c": "000000"})
    assert [node.get(qn("w:val")) for node in root(output).iter(qn("w:color"))] == ["ff0000"]


def test_relative_html_fallback_uses_loop_scope(engine: Engine) -> None:
    template = document(paragraph("{d.rows[i].h:ifEmpty(.fb):html}") + paragraph("{d.rows[i+1].h}"))
    assert text(engine.render(template, {"rows": [{"h": "", "fb": "<b>fallback</b>"}]})) == "fallback"


@pytest.mark.parametrize("data", [{"missing": None}, {}])
def test_missing_non_item_path_in_loop(engine: Engine, data: dict) -> None:
    template = document(
        paragraph("{d.rows[i].x}:{d.missing.child:ifEQ(undefined):show('U'):elseShow('N')}")
        + paragraph("{d.rows[i+1].x}")
    )
    assert text(engine.render(template, {"rows": [{"x": "A"}], **data})) == "A:U"


@pytest.mark.parametrize("tree", [False, True])
def test_failed_nested_loop_restores_state_and_same_engine_recovers(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tree: bool
) -> None:
    from copy import deepcopy
    from dataclasses import replace

    from carbotek.docx.loops import COPY_FEATURES
    from carbotek.errors import FormatterError
    from carbotek.formatters import default_registry

    seen: list[str] = []
    registry = default_registry()

    def probe(value: str) -> str:
        assert CURRENT_RENDER.get() is not None
        assert COPY_FEATURES.get() is not None
        seen.append(value)
        if value == "boom":
            raise ValueError("late loop failure")
        return value

    registry.register("testLoopProbe", probe)
    monkeypatch.setattr(engine, "_document", replace(engine._document, renderer=DocxTextRenderer(registry)))
    bookmark = '<w:bookmarkStart w:id="1" w:name="inner"/>' if tree else ""
    end_bookmark = '<w:bookmarkEnd w:id="1"/>' if tree else ""
    template = document(
        paragraph("{d.seed:set(d.target)}")
        + paragraph("{d.groups[i].name}")
        + paragraph("{d.groups[i].show:showBegin}")
        + "<w:p>"
        + bookmark
        + "<w:r><w:t>{d.groups[i].rows[j].value:testLoopProbe}</w:t></w:r>"
        + end_bookmark
        + "</w:p>"
        + paragraph("{d.groups[i].rows[j+1].value}")
        + paragraph("{d.groups[i].show:showEnd}")
        + paragraph("{d.groups[i+1].name}")
        + paragraph("AFTER")
    )
    data = {
        "seed": "new",
        "target": "original",
        "groups": [
            {"name": "A", "show": True, "rows": [{"value": "first"}, {"value": "second"}]},
            {"name": "B", "show": True, "rows": [{"value": "boom"}]},
        ],
    }
    original = deepcopy(data)
    for _ in range(2):
        seen.clear()
        with pytest.raises(FormatterError, match="late loop failure"):
            engine.render(template, data, {"complement": {"secret": "FAILED_RENDER"}})
        assert seen == ["first", "second", "boom"]
        assert data == original
        assert CURRENT_RENDER.get() is None
        assert COPY_FEATURES.get() is None
        healthy = deepcopy(data)
        healthy["groups"][1]["rows"][0]["value"] = "third"
        seen.clear()
        assert text(engine.render(template, healthy)) == "AfirstsecondBthirdAFTER"
        assert seen == ["first", "second", "third"]
        assert healthy["target"] == "original"
        assert CURRENT_RENDER.get() is None
        assert COPY_FEATURES.get() is None
        output = engine.render(
            document(paragraph("VISIBLE:{d.literal}/{c.secret}")),
            {"literal": "{c.secret}"},
            {"complement": {"secret": "FRESH"}},
        )
        assert text(output) == "VISIBLE:{c.secret}/FRESH"


@pytest.mark.parametrize("explicit", [False, True])
def test_image_environment_is_snapshotted_before_formatters(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool
) -> None:
    from dataclasses import replace

    from PIL import Image

    from carbotek.features.fetch import ImageSourceConfig
    from carbotek.formatters import default_registry

    initial = tmp_path / "initial"
    later = tmp_path / "later"
    initial.mkdir()
    later.mkdir()
    image = initial / "source.png"
    Image.new("RGB", (3, 2), "red").save(image)
    payload = image.read_bytes()
    monkeypatch.setenv("CARBOTEK__IMAGE_FILE_ROOT", str(initial))
    registry = default_registry()

    def change_environment(value: str) -> str:
        monkeypatch.setenv("CARBOTEK__IMAGE_FILE_ROOT", str(later))
        return value

    registry.register("testChangeImageEnvironment", change_environment)
    monkeypatch.setattr(
        engine,
        "_document",
        replace(
            engine._document,
            renderer=DocxTextRenderer(registry),
            image_source_config=ImageSourceConfig(file_root=str(initial)) if explicit else None,
        ),
    )
    picture = (
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="100" cy="100"/>'
        '<wp:docPr id="1" name="Picture" descr="{d.picture}"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdTemplate"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    template = document(paragraph("{d.name:testChangeImageEnvironment}") + picture)
    data = {"name": "IMAGE", "picture": image.as_uri()}
    for attempt in range(2):
        output = engine.render(template, data)
        with ZipFile(BytesIO(output)) as archive:
            media = [archive.read(name) for name in archive.namelist() if name.startswith("word/media/CarbotekImage")]
        assert len(media) == 1
        assert (media[0] == payload) is (explicit or attempt == 0)


@pytest.mark.parametrize("setting", ["MAX_BYTES", "FETCH_TIMEOUT_MS", "MAX_DOWNLOAD_CONCURRENCY", "TOTAL_TIMEOUT_MS"])
def test_invalid_image_environment_has_typed_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv("CARBOTEK__IMAGE_" + setting, "invalid")
    with pytest.raises(ValidationError, match="Image source environment limits"):
        engine.render(document(paragraph("{d.name}")), {"name": "VALUE"})


def test_hidden_block_compiled_fields_keep_side_effects_and_literal_values(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from carbotek.formatters import default_registry

    seen: list[str] = []
    registry = default_registry()

    def record(value: str) -> str:
        seen.append(value)
        return value

    registry.register("testHiddenProbe", record)
    monkeypatch.setattr(engine, "_document", replace(engine._document, renderer=DocxTextRenderer(registry)))
    template = document(
        paragraph("{d.rows[i].name}")
        + paragraph("{d.rows[i].show:showBegin}")
        + paragraph("VALUE={d.rows[i].value:testHiddenProbe}")
        + paragraph("{d.rows[i].show:showEnd}")
        + paragraph("{d.rows[i+1].name}")
        + paragraph("AFTER")
    )
    output = engine.render(
        template,
        {
            "secret": "LEAK",
            "rows": [
                {"name": "A", "show": False, "value": "{c.secret}"},
                {"name": "B", "show": True, "value": "{d.secret}"},
            ],
        },
        {"complement": {"secret": "LEAK"}},
    )
    assert seen == ["{c.secret}", "{d.secret}"]
    assert text(output) == "ABVALUE={d.secret}AFTER"


def test_nested_tree_loop_values_are_literal(engine: Engine) -> None:
    body = (
        paragraph("{d.groups[i].name}")
        + '<w:p><w:bookmarkStart w:id="1" w:name="item"/><w:r><w:t>{d.groups[i].rows[j].x}</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
        + paragraph("{d.groups[i].rows[j+1].x}")
        + paragraph("{d.groups[i+1].name}")
    )
    output = engine.render(document(body), {"groups": [{"name": "G", "rows": [{"x": "{d.secret}"}]}], "secret": "LEAK"})
    assert text(output) == "G{d.secret}"


@pytest.mark.parametrize("call", ["drop('p')", "drop( p )"])
def test_spaced_and_quoted_directive(engine: Engine, call: str) -> None:
    assert (
        text(engine.render(document(paragraph("remove {d.y:" + call + "}") + paragraph("keep")), {"y": True})) == "keep"
    )


@pytest.mark.parametrize("call", ["drop (p)", "keep (p)", "drop (col)"])
def test_space_before_directive_parenthesis_is_an_unknown_formatter(engine: Engine, call: str) -> None:
    # Carbone 5.14.4: the directive regex has no space before the parenthesis.
    with pytest.raises(UnknownFormatterError, match="does not exist"):
        engine.render(document(paragraph("remove {d.y:" + call + "}")), {"y": True})


def test_alias_enables_html_phase(engine: Engine) -> None:
    output = engine.render(document(paragraph("{$h}")), {"h": "<b>hello</b>"}, {"variable_str": "{#h=d.h:html}"})
    assert text(output) == "hello"


def test_hyperlink_arguments_use_document_data(engine: Engine) -> None:
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="{d.path:prepend(d.base)}" TargetMode="External" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>'
        "</Relationships>"
    )
    body = '<w:p><w:hyperlink r:id="rId1"><w:r><w:t>link</w:t></w:r></w:hyperlink></w:p>'
    output = engine.render(
        document(body, **{"word/_rels/document.xml.rels": relationships}),
        {"path": "x/y", "base": "https://example.com/"},
    )
    assert root(output, "word/_rels/document.xml.rels")[0].get("Target") == "https://example.com/x/y"


def test_chart_formatter_cannot_mutate_caller(engine: Engine) -> None:
    data = {"cfg": {"title": "Chart"}}
    engine.render(document(paragraph("{d.cfg:chart}")), data)
    assert data == {"cfg": {"title": "Chart"}}


def test_loop_bookmark_reference_in_next_paragraph(engine: Engine) -> None:
    body = (
        '<w:p><w:bookmarkStart w:id="1" w:name="item"/><w:r><w:t>{d.rows[i].x}</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
        '<w:p><w:hyperlink w:anchor="item"><w:r><w:t>link</w:t></w:r></w:hyperlink>'
        '<w:fldSimple w:instr=" REF item "><w:r><w:t>field</w:t></w:r></w:fldSimple></w:p>'
        + paragraph("{d.rows[i+1].x}")
    )
    output = root(engine.render(document(body), {"rows": [{"x": "A"}, {"x": "B"}]}))
    assert [node.get(qn("w:anchor")) for node in output.iter(qn("w:hyperlink"))] == ["item", "item_1"]
    assert [node.get(qn("w:instr")) for node in output.iter(qn("w:fldSimple"))] == [" REF item ", " REF item_1 "]


def test_textbox_repeats_inner_paragraphs(engine: Engine) -> None:
    box = (
        "<w:p><w:r><w:pict><w:txbxContent>"
        + paragraph("{d.rows[i].x}")
        + paragraph("{d.rows[i+1].x}")
        + "</w:txbxContent></w:pict></w:r></w:p>"
    )
    output = root(engine.render(document(box), {"rows": [{"x": "A"}, {"x": "B"}]}))
    content = next(output.iter(qn("w:txbxContent")))
    assert ["".join(node.itertext()) for node in content] == ["A", "B"]


def test_hidden_loop_cell_has_a_paragraph(engine: Engine) -> None:
    body = (
        "<w:tbl><w:tblGrid/><w:tr><w:tc>"
        + paragraph("{d.rows[i].show:showBegin}")
        + paragraph("hidden")
        + paragraph("{d.rows[i].show:showEnd}")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + paragraph("{d.rows[i+1].show}")
        + "</w:tc></w:tr></w:tbl>"
    )
    output = root(engine.render(document(body), {"rows": [{"show": False}]}))
    assert all(cell.find(qn("w:p")) is not None for cell in output.iter(qn("w:tc")))


def test_split_option_in_footnote(engine: Engine) -> None:
    footnote = (
        f'<w:footnotes {_NAMESPACES}><w:footnote w:id="1"><w:p>'
        "<w:r><w:t>{o.</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t> lang = e n}</w:t></w:r>"
        "</w:p></w:footnote></w:footnotes>"
    )
    output = engine.render(document(paragraph("{d.n:formatN(1)}"), **{"word/footnotes.xml": footnote}), {"n": 1234.5})
    assert text(output) == "1,234.5"
    assert "{o." not in text(output, "word/footnotes.xml")


def test_private_use_character_survives_loop(engine: Engine) -> None:
    body = paragraph("{d.rows[i].x}\ue001") + paragraph("{d.rows[i+1].x}")
    assert text(engine.render(document(body), {"rows": [{"x": "\ue001"}]})) == "\ue001\ue001"


def test_standalone_renderer_values_are_literal() -> None:
    source = f"<w:document {_NAMESPACES}><w:body>{paragraph('{d.rows[i].x}/{d.rows[i+1].x}')}</w:body></w:document>"
    tree = DocxTextRenderer().render_tree(
        source.encode(), {"rows": [{"x": "{d.secret}"}], "secret": "LEAK"}, RenderOptions()
    )
    assert "".join(tree.itertext()) == "{d.secret}/"


@pytest.mark.parametrize(
    "options",
    [
        {"lang": 1},
        {"timezone": []},
        {"variable_str": None},
        {"use_high_precision_arithmetic": "false"},
        {"unknown": True},
        {"complement": []},
        {"translations": {"en": []}},
    ],
)
def test_option_validation_is_typed(options: dict) -> None:
    with pytest.raises(ValidationError):
        RenderOptions.create(**options)


def test_direct_options_copy_and_freeze_mappings(engine: Engine) -> None:
    translations = {"ru": {"hello": "Привет"}}
    options = RenderOptions(translations=translations)
    translations["ru"]["hello"] = "changed"
    assert text(engine.render(document(paragraph("{t(hello)}")), {}, options)) == "Привет"
    with pytest.raises(TypeError):
        options.translations["ru"]["hello"] = "changed"


def test_direct_options_get_now_on_render(engine: Engine) -> None:
    options = RenderOptions()
    assert "T" in text(engine.render(document(paragraph("{c.now}")), {}, options))
    assert "now" not in options.complement


def test_regional_locale_uses_language_number_rules(engine: Engine) -> None:
    assert text(engine.render(document(paragraph("{d.n:formatN(1)}")), {"n": 1234.5}, {"lang": "de-DE"})) == "1.234,5"


def test_loop_keeps_local_namespace_binding(engine: Engine) -> None:
    body = '<w:p xmlns:w14="urn:test:local" w14:paraId="A"><w:r><w:t>{d.rows[i].x}</w:t></w:r></w:p>' + paragraph(
        "{d.rows[i+1].x}"
    )
    output = root(engine.render(document(body), {"rows": [{"x": "A"}, {"x": "B"}]}))
    assert len([node for node in output.iter(qn("w:p")) if node.get("{urn:test:local}paraId") == "A"]) == 2


def test_simultaneous_cold_renders_share_compilation(monkeypatch: pytest.MonkeyPatch) -> None:
    from carbotek.pipeline import document as pipeline

    original = pipeline.compile_template
    barrier = Barrier(8)
    delay = Event()
    calls: list[int] = []

    def compile_once(*args, **kwargs):
        calls.append(1)
        delay.wait(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "compile_template", compile_once)
    engine = Engine()
    template = document(paragraph("{d.name}"))

    def render(index: int) -> str:
        barrier.wait(timeout=5)
        return text(engine.render(template, {"name": str(index)}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(render, range(8)))
    assert results == [str(index) for index in range(8)]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("{d.x:replace('a','b)}", "b"),
        ("{d.x:padl(5,'0)}", "0000a"),
        ("{d.x:replace('a','b,c)}", "b"),
        ("{d.x:append(' b,c)}", "ab"),
        ("{d.x:append(' b):upperCase}", "ab"),
        ("{d.x:replace('a',' b,c)}", "b"),
        ("{d.x:replace(a,'b,c):upperCase}", "b"),
        ("{d.x:replace('a,b,c)}", "b"),
        ("{d.x:padl(5,' 0)}", "0000a"),
        ("{d.x:append(' b):append(c)}", "ab):append(c"),
        ('{d.x:append(" b)}', 'a"b'),
        ("{d.x:append(' b}", "ab"),
        ("{d.x:replace('a','b,c,d)}", "b"),
    ],
)
def test_unterminated_formatter_arguments(engine: Engine, marker: str, expected: str) -> None:
    assert text(engine.render(document(paragraph(marker)), {"x": "a"})) == expected


def test_chart_embedded_workbook_is_prepared_once_and_render_copies_are_independent(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carbotek.features import native_chart

    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    rel_ns = NAMESPACES["r"]
    book = BytesIO()
    with ZipFile(book, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{sheet_ns}" xmlns:r="{rel_ns}"><sheets><sheet name="Sheet1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{sheet_ns}"><dimension ref="A1:A2"/><sheetData><row r="1"><c r="A1"><v>100</v></c></row><row r="2"><c r="A2"><v>200</v></c></row></sheetData></worksheet>',
        )
        archive.writestr("unchanged.bin", b"preserved")
    chart = (
        f'<c:chartSpace xmlns:c="{chart_ns}" xmlns:r="{rel_ns}"><c:chart><c:plotArea><c:barChart><c:ser>'
        '<c:val><c:numRef><c:f>Sheet1!$A$1:$A$2</c:f><c:numCache><c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>{d.groups[i].values[i].n}</c:v></c:pt>'
        '<c:pt idx="1"><c:v>{d.groups[i].values[i+1].n}</c:v></c:pt>'
        '</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart><c:externalData r:id="rIdBook"/></c:chartSpace>'
    )
    template = document(
        paragraph("{d.groups[i].name}")
        + f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>'
        + paragraph("{d.groups[i+1].name}"),
        **{
            "word/charts/chart1.xml": chart,
            "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{rel_ns}/chart"/></Relationships>',
            "word/charts/_rels/chart1.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdBook" Target="../embeddings/book.xlsx" Type="{rel_ns}/package"/></Relationships>',
        },
    )
    stream = BytesIO()
    with ZipFile(BytesIO(template)) as source, ZipFile(stream, "w", ZIP_DEFLATED) as destination:
        for info in source.infolist():
            destination.writestr(info, source.read(info))
        destination.writestr("word/embeddings/book.xlsx", book.getvalue())
    calls = 0
    prepare = native_chart._prepare_workbook

    def counted(payload: bytes) -> native_chart._WorkbookTemplate:
        nonlocal calls
        calls += 1
        return prepare(payload)

    monkeypatch.setattr(native_chart, "_prepare_workbook", counted)
    for shift in (0, 1000, 0):
        output = engine.render(
            stream.getvalue(),
            {
                "groups": [
                    {"name": "A", "values": [{"n": 11 + shift}, {"n": 12 + shift}]},
                    {"name": "B", "values": [{"n": 21 + shift}]},
                ]
            },
        )
        with ZipFile(BytesIO(output)) as archive:
            assert "word/embeddings/book.xlsx" not in archive.namelist()
            books = [archive.read(name) for name in archive.namelist() if "CarbotekWorkbook" in name]
            assert len(books) == 2
            values = []
            for payload in books:
                with ZipFile(BytesIO(payload)) as embedded:
                    assert embedded.read("unchanged.bin") == b"preserved"
                    sheet = etree.fromstring(embedded.read("xl/worksheets/sheet1.xml"))
                    values.append([node.text for node in sheet.iter(f"{{{sheet_ns}}}v")])
            assert values == [[str(11 + shift), str(12 + shift)], [str(21 + shift)]]
    assert calls == 1


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_chart_cleanup_preserves_shared_workbook_graph(engine: Engine, shared: bool, empty: bool) -> None:
    import posixpath

    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = NAMESPACES["r"]
    pkg_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    book = BytesIO()
    with ZipFile(book, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{sheet_ns}" xmlns:r="{rel_ns}"><sheets><sheet name="Sheet1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pkg_rel}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{sheet_ns}"><sheetData><row r="1"><c r="A1"><v>100</v></c></row></sheetData></worksheet>',
        )
        archive.writestr("unchanged.bin", b"shared source")
    chart = (
        f'<c:chartSpace xmlns:c="{chart_ns}" xmlns:r="{rel_ns}"><c:chart><c:plotArea><c:barChart><c:ser>'
        '<c:val><c:numRef><c:f>Sheet1!$A$1</c:f><c:numCache><c:ptCount val="1"/>'
        '<c:pt idx="0"><c:v>{d.groups[i].n}</c:v></c:pt>'
        '</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart><c:externalData r:id="rIdBook"/></c:chartSpace>'
    )
    relations = f'<Relationships xmlns="{pkg_rel}"><Relationship Id="rIdBook" Target="../embeddings/book.xlsx" Type="{rel_ns}/package"/></Relationships>'
    body = (
        paragraph("{d.groups[i].name}")
        + f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>'
        + paragraph("{d.groups[i+1].name}")
    )
    parts = {
        "word/charts/chart1.xml": chart,
        "word/charts/_rels/chart1.xml.rels": relations,
        "word/_rels/document.xml.rels": f'<Relationships xmlns="{pkg_rel}"><Relationship Id="rIdChart" Target="charts/chart1.xml" Type="{rel_ns}/chart"/></Relationships>',
    }
    if shared:
        parts.update(
            {
                "word/charts/chart2.xml": chart.replace("{d.groups[i].n}", "100"),
                "word/charts/_rels/chart2.xml.rels": relations,
                "word/header1.xml": f'<w:hdr {_NAMESPACES}><w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdStatic"/></w:drawing></w:r></w:p></w:hdr>',
                "word/_rels/header1.xml.rels": f'<Relationships xmlns="{pkg_rel}"><Relationship Id="rIdStatic" Target="charts/chart2.xml" Type="{rel_ns}/chart"/></Relationships>',
            }
        )
        parts["word/_rels/document.xml.rels"] = parts["word/_rels/document.xml.rels"].replace(
            "</Relationships>",
            f'<Relationship Id="rIdHeader" Target="header1.xml" Type="{rel_ns}/header"/></Relationships>',
        )
    template = document(body, **parts)
    stream = BytesIO()
    with ZipFile(BytesIO(template)) as source, ZipFile(stream, "w", ZIP_DEFLATED) as destination:
        for info in source.infolist():
            content = source.read(info)
            if shared and info.filename == "word/document.xml":
                tree = etree.fromstring(content)
                section = tree.find(f"{qn('w:body')}/{qn('w:sectPr')}")
                assert section is not None
                reference = etree.SubElement(section, qn("w:headerReference"))
                reference.set(qn("w:type"), "default")
                reference.set(f"{{{rel_ns}}}id", "rIdHeader")
                content = etree.tostring(tree)
            if info.filename == "[Content_Types].xml":
                types = etree.fromstring(content)
                for name in (
                    "word/charts/chart1.xml",
                    "word/embeddings/book.xlsx",
                    *(("word/charts/chart2.xml",) if shared else ()),
                ):
                    etree.SubElement(
                        types,
                        f"{{{ct_ns}}}Override",
                        PartName="/" + name,
                        ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
                        if name.endswith(".xml")
                        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                content = etree.tostring(types)
            destination.writestr(info, content)
        destination.writestr("word/embeddings/book.xlsx", book.getvalue())
    output = engine.render(
        stream.getvalue(), {"groups": [] if empty else [{"name": "A", "n": 11}, {"name": "B", "n": 21}]}
    )
    with ZipFile(BytesIO(output)) as archive:
        names = set(archive.namelist())
        assert "word/charts/chart1.xml" not in names
        assert "word/charts/_rels/chart1.xml.rels" not in names
        assert ("word/embeddings/book.xlsx" in names) is shared
        if shared:
            assert archive.read("word/embeddings/book.xlsx") == book.getvalue()
            assert (
                root(output, "word/header1.xml").find(f".//{{{chart_ns}}}chart").get(f"{{{rel_ns}}}id") == "rIdStatic"
            )
            assert "word/charts/chart2.xml" in names
        generated = sorted(name for name in names if "CarbotekWorkbook" in name)
        assert len(generated) == (0 if empty else 2)
        for name, value in zip(generated, ("11", "21"), strict=not empty):
            with ZipFile(BytesIO(archive.read(name))) as embedded:
                assert embedded.read("unchanged.bin") == b"shared source"
                sheet = etree.fromstring(embedded.read("xl/worksheets/sheet1.xml"))
                assert [node.text for node in sheet.iter(f"{{{sheet_ns}}}v")] == [value]
        for name in names:
            if name.endswith(".rels"):
                directory, filename = name.rsplit("_rels/", 1)
                owner = directory + filename.removesuffix(".rels")
                for relation in etree.fromstring(archive.read(name)):
                    if relation.get("TargetMode") != "External":
                        target = posixpath.normpath(
                            posixpath.join(posixpath.dirname(owner), relation.get("Target", ""))
                        ).lstrip("/")
                        assert target in names, (name, target)
        types = etree.fromstring(archive.read("[Content_Types].xml"))
        assert all(node.get("PartName").lstrip("/") in names for node in types.iter(f"{{{ct_ns}}}Override"))


def test_blocks_outside_table_keep_string_section_opaque(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from carbotek.docx.strings import StringSection

    def unexpected_parse(*args: object) -> None:
        raise AssertionError("A block outside the cell must not materialize the section")

    monkeypatch.setattr(StringSection, "parse", unexpected_parse)
    template = document(
        paragraph("{d.rows[i].name}")
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("CELL")
        + "</w:tc></w:tr></w:tbl>"
        + paragraph("{d.rows[i].show:showBegin}")
        + paragraph("NOTE")
        + paragraph("{d.rows[i].show:showEnd}")
        + paragraph("{d.rows[i+1].name}")
    )
    output = engine.render(template, {"rows": [{"name": "A", "show": False}, {"name": "B", "show": True}]})
    assert text(output) == "ACELLBCELLNOTE"
    assert all(cell.find(qn("w:p")) is not None for cell in root(output).iter(qn("w:tc")))


@pytest.mark.parametrize(
    "source_rate,expected",
    [
        ("0o10", "2"),
        ("0b10", "8"),
        ("0", "Infinity"),
        ("-0", "-Infinity"),
        ("1_000", "NaN"),
        ("inf", "NaN"),
        ([], "Infinity"),
        ([2], "8"),
        ({}, "NaN"),
        (0, "16"),
        (False, "16"),
        ("", "16"),
    ],
)
def test_currency_rate_uses_javascript_number_coercion(engine: Engine, source_rate: object, expected: str) -> None:
    options = {"currency": {"source": "USD", "target": "EUR", "rates": {"USD": source_rate, "EUR": 2}}}
    assert text(engine.render(document(paragraph("{d.value:convCurr}")), {"value": 8}, options)) == expected


@pytest.mark.parametrize("value,target_rate,expected", [(0, 2, "NaN"), (8, "0", "NaN"), (8, [], "NaN")])
def test_zero_currency_rate_has_javascript_nonfinite_result(
    engine: Engine, value: int, target_rate: object, expected: str
) -> None:
    options = {"currency": {"source": "USD", "target": "EUR", "rates": {"USD": "0", "EUR": target_rate}}}
    assert text(engine.render(document(paragraph("{d.value:convCurr}")), {"value": value}, options)) == expected


@pytest.mark.parametrize(
    "value,call,expected",
    [
        ("1", "add(0):convCurr('EUR','USD'):formatN(30)", "0.666666666666666666660000000000"),
        ("1", "div(3):formatN(30)", "0.333333333333333333330000000000"),
        ("999999999999999999999999999999", "add(1):formatN(0)", "1,000,000,000,000,000,000,000,000,000,000"),
        ("123456789012345678901234567890", "div(3):formatN(2)", "41,152,263,004,115,226,300,411,522,630.00"),
        ("999999999999999999999999999999", "mul(9):formatN(0)", "8,999,999,999,999,999,999,999,999,999,991"),
    ],
)
def test_high_precision_arithmetic_ignores_decimal_global_precision(
    engine: Engine, value: str, call: str, expected: str
) -> None:
    from decimal import Inexact, localcontext

    with localcontext() as decimal_context:
        decimal_context.prec = 3
        decimal_context.traps[Inexact] = True
        assert (
            text(
                engine.render(
                    document(paragraph("{d.value:" + call + "}")),
                    {"value": value},
                    {"lang": "en", "use_high_precision_arithmetic": True, "currency": {"rates": {"USD": 3, "EUR": 2}}},
                )
            )
            == expected
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.00", "1"),
        ("-0", "0"),
        ("0.000001", "0.000001"),
        ("0.0000001", "1e-7"),
        ("100000000000000000000", "100000000000000000000"),
        ("1000000000000000000000", "1e+21"),
        ("123456789012345678901234567890", "1.2345678901234567890123456789e+29"),
    ],
)
def test_high_precision_result_prints_like_big_js(engine: Engine, value: str, expected: str) -> None:
    assert (
        text(
            engine.render(
                document(paragraph("{d.value:add(0)}")), {"value": value}, {"use_high_precision_arithmetic": True}
            )
        )
        == expected
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (" 1", "NaN"),
        ("+1", "NaN"),
        ("1_000", "NaN"),
        ("\u0661", "NaN"),
        ("1.", "1"),
        (".5", "0.5"),
        ("1e3", "1000"),
        (True, "NaN"),
        (None, ""),
        ([], "NaN"),
        ([2], "2"),
        ({}, "NaN"),
        ("Infinity", "NaN"),
    ],
)
def test_high_precision_operand_uses_big_js_grammar(engine: Engine, value: object, expected: str) -> None:
    assert (
        text(
            engine.render(
                document(paragraph("{d.value:add(0)}")), {"value": value}, {"use_high_precision_arithmetic": True}
            )
        )
        == expected
    )


@pytest.mark.parametrize("value", [8, "8", [], {}])
def test_high_precision_currency_requires_big_result(engine: Engine, value: object) -> None:
    from carbotek.errors import FormatterError

    with pytest.raises(FormatterError, match=r"d\.div is not a function"):
        engine.render(
            document(paragraph("{d.value:convCurr}")),
            {"value": value},
            {"use_high_precision_arithmetic": True, "currency": {"source": "USD", "target": "EUR"}},
        )


def test_high_precision_currency_without_source_keeps_raw_value(engine: Engine) -> None:
    assert (
        text(
            engine.render(
                document(paragraph("{d.value:convCurr}")),
                {"value": 8},
                {"use_high_precision_arithmetic": True, "currency": {"source": None}},
            )
        )
        == "8"
    )


def test_renderer_caches_evict_unique_template_marker_plans() -> None:
    caches = (
        renderer_module._marker_plan,
        renderer_module._aggregate_shape,
        renderer_module._compiled_marker,
        renderer_module._loop_item_plan,
        loop_module._cached_boundary,
    )
    registry = default_registry()
    for cache in caches:
        cache.cache_clear()
    try:
        for index in range(2200):
            raw = "{d.rows[i].field" + str(index) + "}"
            marker = parse(raw)
            renderer_module._marker_plan(raw)
            renderer_module._aggregate_shape(marker)
            renderer_module._compiled_marker(registry, marker)
            renderer_module._loop_item_plan(marker.path)
            loop_module._cached_boundary(raw, ())
        for cache in caches:
            info = cache.cache_info()
            assert info.maxsize == 2048 and info.currsize == 2048
            assert info.misses == 2200
        misses = renderer_module._marker_plan.cache_info().misses
        renderer_module._marker_plan("{d.rows[i].field0}")
        assert renderer_module._marker_plan.cache_info().misses == misses + 1
    finally:
        for cache in caches:
            cache.cache_clear()


def test_feature_loop_rows_do_not_add_parser_or_renderer_cache_keys(engine: Engine) -> None:
    template = document(paragraph("{d.rows[i].h:html(inline)}") + paragraph("{d.rows[i+1].h:html(inline)}"))
    caches = (
        parse,
        renderer_module._marker_plan,
        renderer_module._aggregate_shape,
        renderer_module._compiled_marker,
        renderer_module._loop_item_plan,
        loop_module._cached_boundary,
    )
    small = engine.render(template, {"rows": [{"h": "<b>A</b>"}] * 3})
    assert text(small) == "AAA"
    sizes = [cache.cache_info().currsize for cache in caches]
    large = engine.render(template, {"rows": [{"h": "<b>B</b>"}] * 200})
    assert text(large) == "B" * 200
    assert [cache.cache_info().currsize for cache in caches] == sizes


@pytest.mark.parametrize("kind", ["footnote", "endnote"])
def test_note_html_color_and_options_use_document_session(engine: Engine, kind: str) -> None:
    name = "word/" + kind + "s.xml"
    note = (
        f'<w:{kind}s {_NAMESPACES}><w:{kind} w:id="1">'
        + paragraph("{o. lang = e n}")
        + paragraph("{d.h:html}")
        + paragraph("TINT{c.tint:color(p)}")
        + paragraph("{d.n:formatN(1)}")
        + f"</w:{kind}></w:{kind}s>"
    )
    body = (
        paragraph("{d.source:set(d.h)}{d.color:set(c.tint)}") + f'<w:p><w:r><w:{kind}Reference w:id="1"/></w:r></w:p>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rIdNote" Target="{kind}s.xml" Type="{NAMESPACES["r"]}/{kind}s"/>'
        "</Relationships>"
    )
    data = {"source": "<p>NOTE {d.secret}</p>", "color": "FF0000", "n": 1234.5, "secret": "LEAK"}
    output = engine.render(
        document(body, **{name: note, "word/_rels/document.xml.rels": relationships}),
        data,
        {"lang": "ru", "complement": {"tint": "00FF00"}},
    )
    assert text(output, name) == "NOTE {d.secret}TINT1,234.5"
    assert [node.get(qn("w:val")) for node in root(output, name).iter(qn("w:color"))] == ["ff0000"]
    assert data == {"source": "<p>NOTE {d.secret}</p>", "color": "FF0000", "n": 1234.5, "secret": "LEAK"}
    assert "LEAK" not in text(output, name)


@pytest.mark.parametrize("value", ["scalar", 7, True])
def test_scalar_parent_is_undefined_in_text_loops_and_html(engine: Engine, value: object) -> None:
    condition = ":ifEQ(undefined):show('U'):elseShow('WRONG')"
    template = document(
        paragraph("{d.a.b" + condition + "}")
        + paragraph("{d.rows[i].a.b" + condition + "}")
        + paragraph("{d.rows[i+1].a.b}")
        + paragraph("{d.a.b" + condition + ":html}")
    )
    output = engine.render(template, {"a": value, "rows": [{"a": value}, {"a": value}]})
    assert text(output) == "UUUU"


@pytest.mark.parametrize("failure", ["encrypted", "deflate64"])
@pytest.mark.parametrize("operation", ["compile", "render"])
def test_zip_read_failures_have_template_error_contract(engine: Engine, failure: str, operation: str) -> None:
    damaged = bytearray(document(paragraph("OK")))
    for signature, flags_offset, compression_offset in ((b"PK\x03\x04", 6, 8), (b"PK\x01\x02", 8, 10)):
        position = 0
        while (position := damaged.find(signature, position)) >= 0:
            offset = position + (flags_offset if failure == "encrypted" else compression_offset)
            damaged[offset : offset + 2] = (1 if failure == "encrypted" else 9).to_bytes(2, "little")
            position += 4
    with pytest.raises(InvalidTemplateError) as error:
        if operation == "compile":
            engine.compile(bytes(damaged))
        else:
            engine.render(bytes(damaged), {})
    assert error.value.code == "TEMPLATE_ERROR"
    assert isinstance(error.value.__cause__, (RuntimeError, NotImplementedError))


def test_invalid_barcode_digits_have_formatter_error_contract(engine: Engine) -> None:
    with pytest.raises(FormatterError, match="Barcode generation error") as error:
        engine.render(
            document(
                '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Code" '
                'descr="{d.code:barcode(ean13)}"/><a:graphic><a:graphicData><pic:pic>'
                '<pic:blipFill><a:blip r:embed="rIdPlaceholder"/></pic:blipFill>'
                "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
            ),
            {"code": "12"},
        )
    assert error.value.code == "TEMPLATE_ERROR"
    assert error.value.__cause__ is not None


def test_superscript_css_digits_do_not_escape_html_error_contract(engine: Engine) -> None:
    output = engine.render(
        document(paragraph("{d.h:html}")),
        {"h": '<p style="font-size:²px; padding-left:²px">SAFE</p>'},
    )
    assert text(output) == "SAFE"


@pytest.mark.parametrize("action,condition", [("drop", True), ("keep", False)])
def test_hidden_directive_crossing_loop_end_removes_copy_paragraphs(
    engine: Engine, action: str, condition: bool
) -> None:
    template = document(
        paragraph("{d.rows[i].x}{d.rows[i].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(template, {"rows": [{"x": "A", "remove": condition}, {"x": "B", "remove": condition}]})
    paragraphs = root(output).findall("./w:body/w:p", NAMESPACES)
    assert ["".join(item.itertext()) for item in paragraphs] == ["AFTER"]


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first", [False, True])
@pytest.mark.parametrize("second", [False, True])
@pytest.mark.parametrize("prefix", ["", "PREFIX "])
def test_crossing_directive_repeats_values_and_emits_static_paragraph_once(
    engine: Engine, action: str, first: bool, second: bool, prefix: str
) -> None:
    template = document(
        paragraph(prefix + "{d.rows[i].x}{d.rows[i].remove:" + action + "(p,2)}", properties="<w:rPr><w:b/></w:rPr>")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(
        template,
        {"secret": "LEAK", "rows": [{"x": "A<&{d.secret}", "remove": first}, {"x": "B", "remove": second}]},
    )
    body = root(output).find("w:body", NAMESPACES)
    assert body is not None
    visible = first if action == "keep" else not first
    assert body.text == ("A<&{d.secret}B" if visible else None)
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == (
        [prefix, "AFTER"] if visible else ["AFTER"]
    )
    assert len(body.findall(".//w:b", NAMESPACES)) == int(visible)


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize(
    "chain,rows,expected",
    [
        (":upperCase", [{"x": "a"}, {"x": "b"}], "AB"),
        (":prepend(.prefix)", [{"x": "a", "prefix": "["}, {"x": "b", "prefix": "("}], "[a(b"),
        (":ifEmpty(.fallback)", [{"x": "", "fallback": "A"}, {"x": "", "fallback": "B"}], "AB"),
    ],
)
def test_crossing_directive_formatter_slots_keep_copy_scope(
    engine: Engine, action: str, visible: bool, chain: str, rows: list[dict], expected: str
) -> None:
    template = document(
        paragraph("{d.rows[i].x" + chain + "}{d.rows[i].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    condition = visible if action == "keep" else not visible
    output = engine.render(template, {"rows": [{**row, "remove": condition} for row in rows]})
    body = root(output).find("w:body", NAMESPACES)
    assert body is not None
    assert body.text == (expected if visible else None)
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == (
        ["", "AFTER"] if visible else ["AFTER"]
    )


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first", [False, True])
@pytest.mark.parametrize("second", [False, True])
def test_sorted_crossing_span_uses_first_input_condition_and_sorted_output(
    engine: Engine, action: str, first: bool, second: bool
) -> None:
    template = document(
        paragraph("{d.rows[ord].x}{d.rows[ord].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[ord+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(
        template, {"rows": [{"x": "B", "ord": 2, "remove": first}, {"x": "A", "ord": 1, "remove": second}]}
    )
    body = root(output).find("w:body", NAMESPACES)
    assert body is not None
    visible = first if action == "keep" else not first
    assert body.text == ("AB" if visible else None)
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == (
        ["", "AFTER"] if visible else ["AFTER"]
    )


def test_sorted_crossing_formatter_calls_follow_input_order_once(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    seen: list[object] = []
    registry = default_registry()

    def probe(value: object) -> object:
        seen.append(value)
        return value

    registry.register("testOrderProbe", probe)
    monkeypatch.setattr(engine, "_document", replace(engine._document, renderer=DocxTextRenderer(registry)))
    template = document(
        paragraph("{d.rows[ord].x:testOrderProbe}{d.rows[ord].remove:testOrderProbe:drop(p,2)}")
        + paragraph("{d.rows[ord+1].x}")
        + paragraph("AFTER")
    )
    output = engine.render(
        template, {"rows": [{"x": "B", "ord": 2, "remove": False}, {"x": "A", "ord": 1, "remove": True}]}
    )
    assert seen == [False, "B", True, "A"]
    assert root(output).find("w:body", NAMESPACES).text == "AB"


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize(
    "begin,end,rows,expected",
    [
        (
            "i,active=true",
            "i+1,active=true",
            [
                {"x": "X", "active": False, "remove": True},
                {"x": "A", "active": True, "remove": False},
                {"x": "B", "active": True, "remove": True},
            ],
            "AB",
        ),
        ("i,active=true", "i+1,active=true", [{"x": "X", "active": False, "remove": True}], None),
        ("i*qty", "i+1*qty", [{"x": "A", "qty": 2, "remove": False}, {"x": "B", "qty": 1, "remove": True}], "AAB"),
        ("i*qty", "i+1*qty", [{"x": "X", "qty": 0, "remove": True}, {"x": "A", "qty": 2, "remove": False}], "AA"),
        ("i*qty", "i+1*qty", [{"x": "X", "qty": 0, "remove": True}], None),
    ],
)
def test_crossing_filters_and_repeaters_preserve_selected_condition(
    engine: Engine, action: str, begin: str, end: str, rows: list[dict], expected: str | None
) -> None:
    template = document(
        paragraph("{d.rows[" + begin + "].x}{d.rows[" + begin + "].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[" + end + "].x}")
        + paragraph("AFTER")
    )
    data = [{**row, "remove": not row["remove"] if action == "keep" else row["remove"]} for row in rows]
    body = root(engine.render(template, {"rows": data})).find("w:body", NAMESPACES)
    assert body is not None
    assert body.text == expected
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == (
        ["", "AFTER"] if expected is not None else ["AFTER"]
    )


def test_crossing_repeater_counts_validate_before_formatter_calls(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from carbotek.errors import MarkerResolutionError

    seen: list[object] = []
    registry = default_registry()

    def probe(value: object) -> object:
        seen.append(value)
        return value

    registry.register("testRepeatProbe", probe)
    monkeypatch.setattr(engine, "_document", replace(engine._document, renderer=DocxTextRenderer(registry)))
    template = document(
        paragraph("{d.rows[i*qty].x:testRepeatProbe}{d.rows[i*qty].remove:drop(p,2)}")
        + paragraph("{d.rows[i+1*qty].x}")
        + paragraph("AFTER")
    )
    with pytest.raises(MarkerResolutionError, match="not an integer"):
        engine.render(
            template, {"rows": [{"x": "A", "qty": 1, "remove": False}, {"x": "B", "qty": "bad", "remove": False}]}
        )
    assert seen == []
    repeated = {"x": "A", "qty": 1, "remove": False}
    output = engine.render(template, {"rows": [repeated, repeated]})
    assert seen == ["A", "A"]
    assert root(output).find("w:body", NAMESPACES).text == "AA"


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("copies", [1, 2])
def test_nested_span_closes_inside_each_outer_copy(
    engine: Engine, action: str, first: bool, second: bool, copies: int
) -> None:
    section = (
        paragraph("{d.rows[i].items[j].x}{d.rows[i].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[i].items[j+1].x}")
        + paragraph("{d.rows[i+1].items[j].x}")
    )
    template = document(section * copies + paragraph("AFTER"))
    rows = [
        {"remove": first, "items": [{"x": "A", "j": 0}, {"x": "B", "j": 1}]},
        {"remove": second, "items": [{"x": "C", "j": 0}, {"x": "D", "j": 1}]},
    ]
    expected = []
    for flag, values in [(first, ["A", "B"]), (second, ["C", "D"])]:
        if flag == (action == "keep"):
            expected.extend(values)
    body = root(engine.render(template, {"rows": rows})).find("w:body", NAMESPACES)
    assert body is not None
    assert body.text is None
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == expected * copies + ["AFTER"]


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_crossing_html_keeps_feature_paragraphs_and_first_condition(
    engine: Engine, action: str, first: bool, second: bool
) -> None:
    template = document(
        paragraph("{d.rows[i].x:html}{d.rows[i].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    body = root(
        engine.render(template, {"rows": [{"x": "<b>A</b>", "remove": first}, {"x": "<i>B</i>", "remove": second}]})
    ).find("w:body", NAMESPACES)
    assert body is not None
    visible = first == (action == "keep")
    assert body.text is None
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == (
        ["A", "B", "AFTER"] if visible else ["AFTER"]
    )
    assert len(body.findall(".//w:b", NAMESPACES)) == int(visible)
    assert len(body.findall(".//w:i", NAMESPACES)) == int(visible)


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("flags", [None, (False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("prefix", ["", "PREFIX "])
def test_crossing_static_attribute_copies_stay_outside_hidden_value_span(
    engine: Engine, action: str, flags: tuple[bool, bool] | None, prefix: str
) -> None:
    first = paragraph(prefix + "{d.rows[i].x}{d.rows[i].remove:" + action + "(p,2)}").replace(
        "<w:r>", '<w:r w:rsidR="{d.label}">'
    )
    template = document(first + paragraph("{d.rows[i+1].x}") + paragraph("AFTER"))
    rows = [] if flags is None else [{"x": "A", "remove": flags[0]}, {"x": "B", "remove": flags[1]}]
    body = root(engine.render(template, {"label": "11223344", "rows": rows})).find("w:body", NAMESPACES)
    assert body is not None
    visible = flags is not None and flags[0] == (action == "keep")
    assert body.text == ("AB" if visible else None)
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == [prefix] * len(rows) + ["AFTER"]
    assert body.xpath(".//w:r/@w:rsidR", namespaces=NAMESPACES) == ["11223344"] * len(rows)


@pytest.mark.parametrize("first,second", [("drop", "drop"), ("drop", "keep"), ("keep", "drop"), ("keep", "keep")])
@pytest.mark.parametrize("flags", [None, (False, False), (False, True), (True, False), (True, True)])
def test_coincident_crossing_spans_follow_js_nested_branch_removal(
    engine: Engine, first: str, second: str, flags: tuple[bool, bool] | None
) -> None:
    template = document(
        paragraph("{d.rows[i].x}{d.rows[i].a:" + first + "(p,2)}{d.rows[i].b:" + second + "(p,2)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    rows = (
        []
        if flags is None
        else [
            {"x": "A", "a": flags[0], "b": flags[1]},
            {"x": "B", "a": not flags[0], "b": not flags[1]},
        ]
    )
    body = root(engine.render(template, {"rows": rows})).find("w:body", NAMESPACES)
    assert body is not None
    assert body.text is None
    assert ["".join(item.itertext()) for item in body.findall("w:p", NAMESPACES)] == ["AFTER"]


@pytest.mark.parametrize("first", ["a", "b"])
def test_coincident_spans_evaluate_only_first_source_control(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    from dataclasses import replace

    seen: list[object] = []
    registry = default_registry()

    def probe(value: object) -> bool:
        seen.append(value)
        return False

    registry.register("testCoincidentProbe", probe)
    monkeypatch.setattr(engine, "_document", replace(engine._document, renderer=DocxTextRenderer(registry)))
    second = "b" if first == "a" else "a"
    controls = "".join("{d.rows[i]." + field + ":testCoincidentProbe:drop(p,2)}" for field in [first, second])
    template = document(
        paragraph("{d.rows[i].x:testCoincidentProbe}" + controls) + paragraph("{d.rows[i+1].x}") + paragraph("AFTER")
    )
    output = engine.render(
        template,
        {"rows": [{"x": "X", "a": "A0", "b": "B0"}, {"x": "Y", "a": "A1", "b": "B1"}]},
    )
    assert text(output) == "AFTER"
    assert seen == [first.upper() + "0", first.upper() + "1"]


@pytest.mark.parametrize("marker_root", ["d", "c"])
@pytest.mark.parametrize(
    "path,values,classification",
    [
        ("a.child", {}, "U"),
        ("a.child", {"a": None}, "U"),
        ("a.child", {"a": 7}, "U"),
        ("a", {"a": None}, "N"),
        ("items[3].child", {"items": [{"child": "X"}]}, "U"),
        ("items[0].child", {"items": [None]}, "U"),
        ("items[0].child", {"items": [7]}, "U"),
        ("a.items[0].child", {"a": None}, "U"),
        ("a.items[0].child", {"a": 7}, "U"),
        ("a.items[code=A].child", {"a": None}, "U"),
        ("a.items[code=A].child", {"a": 7}, "U"),
        ("items[0]", {"items": [None]}, "N"),
        ("items[code=A].child", {"items": [{"code": "A", "child": None}]}, "N"),
        ("items[code=Z].child", {"items": [{"code": "A", "child": "X"}]}, "U"),
        ("items[code=A].child.more", {"items": [{"code": "A", "child": None}]}, "U"),
        ("items[code=A].child.more", {"items": [{"code": "A", "child": 7}]}, "U"),
    ],
)
def test_missing_selector_paths_distinguish_null_on_every_evaluation_path(
    engine: Engine, marker_root: str, path: str, values: dict, classification: str
) -> None:
    def check(location: str, *, html: bool = False) -> str:
        suffix = ":html" if html else ""
        return (
            "{"
            + location
            + ":ifEQ(undefined):show('U'):elseShow('N')"
            + suffix
            + "}"
            + "{"
            + location
            + ":ifNE(undefined):show('N'):elseShow('U')"
            + suffix
            + "}"
        )

    location = marker_root + "." + path
    row = marker_root + ".rows[i]"
    template = document(
        paragraph(check(location))
        + paragraph("{" + row + ".x}:" + check(location) + ":" + check(row + "." + path))
        + paragraph("{" + marker_root + ".rows[i+1].x}")
        + paragraph(check(location, html=True))
    )
    data = {**values, "rows": [{"x": "A", **values}, {"x": "B", **values}]}
    output = (
        engine.render(template, data)
        if marker_root == "d"
        else engine.render(template, {"a": "DECOY", "items": [{"child": "DECOY"}]}, {"complement": data})
    )
    pair = classification * 2
    assert text(output) == pair + "A:" + pair + ":" + pair + "B:" + pair + ":" + pair + pair


@pytest.mark.parametrize(
    "data,chain,rates,message,cause",
    [
        ({}, "convCurr", {"USD": 3, "EUR": 2}, "Cannot read properties of undefined (reading 'div')", TypeError),
        ({}, "add(0):convCurr", {"USD": 3, "EUR": 2}, "Cannot read properties of undefined (reading 'div')", TypeError),
        (
            {"value": None},
            "convCurr",
            {"USD": 3, "EUR": 2},
            "Cannot read properties of null (reading 'div')",
            TypeError,
        ),
        (
            {"value": None},
            "add(0):convCurr",
            {"USD": 3, "EUR": 2},
            "Cannot read properties of null (reading 'div')",
            TypeError,
        ),
        ({"value": 8}, "convCurr", {"USD": 3, "EUR": 2}, "d.div is not a function", TypeError),
        ({"value": "8"}, "formatN(2)", {"USD": 3, "EUR": 2}, "_value.toFixed is not a function", TypeError),
        ({"value": 8}, "add(0):convCurr", {"USD": "bad", "EUR": 2}, "[big.js] Invalid number", ValueError),
        ({"value": 8}, "add(0):convCurr", {"USD": [], "EUR": 2}, "[big.js] Invalid number", ValueError),
        ({"value": 8}, "add(0):convCurr", {"USD": "0", "EUR": 2}, "[big.js] Division by zero", ValueError),
    ],
)
def test_high_precision_error_contract_matches_js(
    engine: Engine, data: dict, chain: str, rates: dict, message: str, cause: type[Exception]
) -> None:
    template = document(paragraph("{o.useHighPrecisionArithmetic=true}{d.value:" + chain + "}"))
    with pytest.raises(FormatterError) as raised:
        engine.render(template, data, {"currency": {"source": "USD", "target": "EUR", "rates": rates}})
    assert str(raised.value) == message
    assert raised.value.code == "TEMPLATE_ERROR"
    assert isinstance(raised.value.__cause__, cause)


@pytest.mark.parametrize("data,expected", [({}, "NaN"), ({"value": None}, "0")])
def test_missing_currency_value_is_undefined_before_number_coercion(engine: Engine, data: dict, expected: str) -> None:
    assert text(engine.render(document(paragraph("{d.value:convCurr}")), data)) == expected


@pytest.mark.parametrize("data,expected", [({}, "U"), ({"value": None}, "N")])
def test_currency_format_preserves_missing_and_null_for_following_conditions(
    engine: Engine, data: dict, expected: str
) -> None:
    template = document(paragraph("{d.value:formatC:ifEQ(undefined):show('U'):elseShow('N')}"))
    assert text(engine.render(template, data)) == expected


@pytest.mark.parametrize("no_cache", [False, True])
def test_cli_bench_format_label_comes_from_package_content(tmp_path: Path, capsys, no_cache: bool) -> None:
    from carbotek.cli import main

    template = tmp_path / "misnamed.xlsx"
    template.write_bytes(document(paragraph("{d.name}")))
    data = tmp_path / "data.json"
    data.write_text('{"name":"VALUE"}', encoding="utf-8")
    arguments = ["bench", str(template), str(data), "-n", "1"]
    if no_cache:
        arguments.append("--no-cache")
    assert main(arguments) == 0
    assert capsys.readouterr().out.startswith("misnamed.xlsx: docx, n=1")


def test_engine_media_bytes_have_one_cache_budget_owner() -> None:
    media = b"M" * 65536
    template = document(paragraph("{d.name}"), **{"word/media/payload.bin": media})
    engine = Engine()
    compiled = engine.compile(template)
    expected = sum(len(part.content) for part in compiled.package.parts.values())
    assert compiled.size == expected
    assert engine.metrics["bytes"] == expected
    assert engine.compile(template) is compiled
    assert engine.metrics["bytes"] == expected
    assert engine.metrics["size"] == 1


@pytest.mark.parametrize("capacity,budget", [(0, 1000000), (64, 0)])
def test_disabled_engine_cache_retains_no_package_bytes(capacity: int, budget: int) -> None:
    engine = Engine(cache_size=capacity, cache_bytes=budget)
    template = document(paragraph("{d.name}"), **{"word/media/payload.bin": b"M" * 65536})
    first = engine.compile(template)
    second = engine.compile(template)
    assert first is not second
    assert text(engine.render(template, {"name": "VALUE"})) == "VALUE"
    assert engine.metrics["bytes"] == engine.metrics["size"] == engine.metrics["hits"] == 0
    assert engine.metrics["misses"] == 3


def test_engine_byte_budget_evicts_media_before_entry_limit() -> None:
    first_template = document(paragraph("{d.first}"), **{"word/media/payload.bin": b"A" * 65536})
    second_template = document(paragraph("{d.second}"), **{"word/media/payload.bin": b"B" * 65536})
    probe = Engine(cache_size=0)
    budget = probe.compile(first_template).size + probe.compile(second_template).size - 1
    engine = Engine(cache_size=64, cache_bytes=budget)
    first = engine.compile(first_template)
    second = engine.compile(second_template)
    assert engine.metrics["size"] == 1
    assert engine.metrics["bytes"] == second.size
    assert engine.compile(second_template) is second
    assert engine.compile(first_template) is not first
    assert engine.metrics["size"] == 1
    assert engine.metrics["bytes"] <= budget


def test_loop_failure_restores_all_slots_inside_same_context(engine: Engine) -> None:
    from contextvars import Context

    from carbotek.docx.dialect import WORD
    from carbotek.docx.directives import DEFERRED_DIRECTIVES
    from carbotek.docx.loops import COPY_FEATURES, KEEP_RENDERED, PART_HAS_BLOCKS, RENDERED_COPIES
    from carbotek.docx.strings import IS_HIDDEN, TEXT_COMPILER

    registry = default_registry()

    def fail(value: object) -> object:
        assert DEFERRED_DIRECTIVES.get() is True
        assert PART_HAS_BLOCKS.get() is False
        raise ValueError("loop formatter failed")

    registry.register("testStateFailure", fail)
    renderer = DocxTextRenderer(registry)
    template = document(
        paragraph("{d.rows[i].value:testStateFailure}{d.rows[i].flag:drop(col)}") + paragraph("{d.rows[i+1].value}")
    )
    with ZipFile(BytesIO(template)) as archive:
        content = archive.read("word/document.xml")

    def verify() -> None:
        with pytest.raises(FormatterError, match="loop formatter failed"):
            renderer._render_tree(
                content,
                {"rows": [{"value": "X", "flag": False}]},
                RenderOptions.create(),
                normalized=False,
                dialect=WORD,
            )
        assert DEFERRED_DIRECTIVES.get() is False
        assert PART_HAS_BLOCKS.get() is True
        assert KEEP_RENDERED.get() is False
        assert COPY_FEATURES.get() is None
        assert RENDERED_COPIES.get() is None
        assert IS_HIDDEN.get() is None
        assert TEXT_COMPILER.get() is None

    Context().run(verify)


@pytest.mark.parametrize("kind", ["whole", "split", "same"])
def test_html_preserves_neighboring_runs_when_marker_fills_text(engine: Engine, kind: str) -> None:
    middle = {
        "whole": "<w:r><w:t>{d.h:html}</w:t></w:r>",
        "split": "<w:r><w:t>{d.h:</w:t></w:r><w:r><w:t>html}</w:t></w:r>",
        "same": "<w:r><w:t>INLINE {d.h:html} TAIL</w:t></w:r>",
    }[kind]
    template = document(
        "<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Description: {d.client} </w:t></w:r>"
        + middle
        + "<w:r><w:rPr><w:i/></w:rPr><w:t> (end)</w:t></w:r></w:p>"
    )
    output = root(engine.render(template, {"client": "CLIENT", "h": "<p>FIRST</p><p>SECOND</p>"}))
    before = "Description: CLIENT " + ("INLINE " if kind == "same" else "")
    after = (" TAIL" if kind == "same" else "") + " (end)"
    assert ["".join(item.itertext()) for item in output.findall("w:body/w:p", NAMESPACES)] == [
        before,
        "FIRST",
        "",
        "SECOND",
        "",
        after,
    ]
    runs = [run for run in output.iter(qn("w:r")) if "".join(run.itertext())]
    assert runs[0].find("w:rPr/w:b", NAMESPACES) is not None
    assert runs[-1].find("w:rPr/w:i", NAMESPACES) is not None


@pytest.mark.parametrize("name", ["item", "b" * 40])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("field_kind", ["simple", "split"])
def test_loop_bookmark_references_preserve_quotes_and_split_instructions(
    engine: Engine, name: str, quoted: bool, field_kind: str
) -> None:
    target = '"' + name + '"' if quoted else name
    instruction = " PAGEREF " + target + " \\h "
    if field_kind == "simple":
        field = (
            '<w:fldSimple w:instr="'
            + escape(instruction, {'"': "&quot;"})
            + '"><w:r><w:t>field</w:t></w:r></w:fldSimple>'
        )
    else:
        split = len(" PAGEREF ") + 2
        field = (
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve">' + escape(instruction[:split]) + "</w:instrText></w:r>"
            '<w:r><w:instrText xml:space="preserve">' + escape(instruction[split:]) + "</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="separate"/><w:t>field</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        )
    body = (
        '<w:p><w:bookmarkStart w:id="1" w:name="'
        + name
        + '"/><w:r><w:t>{d.rows[i].x}</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
        '<w:p><w:hyperlink w:anchor="'
        + name
        + '"><w:r><w:t>link</w:t></w:r></w:hyperlink>'
        + field
        + "</w:p>"
        + paragraph("{d.rows[i+1].x}")
    )
    output = root(engine.render(document(body), {"rows": [{"x": "A"}, {"x": "B"}]}))
    copied = name[:38] + "_1"
    assert [node.get(qn("w:name")) for node in output.iter(qn("w:bookmarkStart"))] == [name, copied]
    assert [node.get(qn("w:anchor")) for node in output.iter(qn("w:hyperlink"))] == [name, copied]
    instructions = []
    for paragraph_node in output.findall("w:body/w:p", NAMESPACES):
        simple = paragraph_node.find("w:fldSimple", NAMESPACES)
        if simple is not None:
            instructions.append(simple.get(qn("w:instr")))
        elif paragraph_node.find(".//w:instrText", NAMESPACES) is not None:
            instructions.append("".join(node.text or "" for node in paragraph_node.iter(qn("w:instrText"))))
    assert instructions == [instruction, instruction.replace(target, '"' + copied + '"' if quoted else copied)]


@pytest.mark.parametrize("outside_first", [False, True])
def test_loop_bookmark_suffix_avoids_existing_template_names(engine: Engine, outside_first: bool) -> None:
    outside = (
        '<w:p><w:bookmarkStart w:id="2" w:name="item_1"/><w:r><w:t>OUTSIDE</w:t></w:r><w:bookmarkEnd w:id="2"/></w:p>'
    )
    loop = (
        '<w:p><w:bookmarkStart w:id="1" w:name="item"/><w:r><w:t>{d.rows[i].x}</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
        '<w:p><w:hyperlink w:anchor="item"><w:r><w:t>link</w:t></w:r></w:hyperlink>'
        '<w:fldSimple w:instr=" REF item "><w:r><w:t>field</w:t></w:r></w:fldSimple></w:p>'
        + paragraph("{d.rows[i+1].x}")
    )
    body = outside + loop if outside_first else loop + outside
    output = root(engine.render(document(body), {"rows": [{"x": "A"}, {"x": "B"}]}))
    names = [node.get(qn("w:name")) for node in output.iter(qn("w:bookmarkStart"))]
    assert names == (["item_1", "item", "item_2"] if outside_first else ["item", "item_2", "item_1"])
    assert [node.get(qn("w:anchor")) for node in output.iter(qn("w:hyperlink"))] == ["item", "item_2"]
    assert [node.get(qn("w:instr")) for node in output.iter(qn("w:fldSimple"))] == [" REF item ", " REF item_2 "]


@pytest.mark.parametrize("sizes", [(2, 2), (0, 2), (2, 0)])
@pytest.mark.parametrize("boxed", [False, True])
def test_nested_loop_bookmark_references_stay_with_their_copy(
    engine: Engine, sizes: tuple[int, int], boxed: bool
) -> None:
    inner = (
        '<w:p><w:bookmarkStart w:id="1" w:name="item"/><w:r><w:t>{d.groups[i].rows[j].x}</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
        '<w:p><w:hyperlink w:anchor="item"><w:r><w:t>link</w:t></w:r></w:hyperlink>'
        '<w:fldSimple w:instr=" REF item "><w:r><w:t>field</w:t></w:r></w:fldSimple></w:p>'
        + paragraph("{d.groups[i].rows[j+1].x}")
    )
    if boxed:
        inner = "<w:p><w:r><w:pict><w:txbxContent>" + inner + "</w:txbxContent></w:pict></w:r></w:p>"
    body = paragraph("{d.groups[i].name}") + inner + paragraph("{d.groups[i+1].name}")
    groups = [
        {"name": f"G{index}", "rows": [{"x": f"{index}:{row}"} for row in range(size)]}
        for index, size in enumerate(sizes)
    ]
    output = root(engine.render(document(body), {"groups": groups}))
    names = [node.get(qn("w:name")) for node in output.iter(qn("w:bookmarkStart"))]
    assert len(names) == sum(sizes)
    assert len(set(names)) == len(names)
    assert [node.get(qn("w:anchor")) for node in output.iter(qn("w:hyperlink"))] == names
    assert [node.get(qn("w:instr")) for node in output.iter(qn("w:fldSimple"))] == [f" REF {name} " for name in names]
    starts = [node.get(qn("w:id")) for node in output.iter(qn("w:bookmarkStart"))]
    ends = [node.get(qn("w:id")) for node in output.iter(qn("w:bookmarkEnd"))]
    assert starts == ends
    assert len(set(starts)) == len(starts)
    if boxed:
        boxes = list(output.iter(qn("w:txbxContent")))
        assert len(boxes) == len(sizes)
        for index, (box, size) in enumerate(zip(boxes, sizes, strict=True)):
            assert len(list(box.iter(qn("w:bookmarkStart")))) == size
            assert ["".join(node.itertext()) for node in box.findall("w:p", NAMESPACES)] == [
                value for row in range(size) for value in (f"{index}:{row}", "linkfield")
            ]


@pytest.mark.parametrize("construction", ["mapping", "direct", "create"])
def test_uppercase_api_language_normalizes_numbers_and_translations(engine: Engine, construction: str) -> None:
    values = {"lang": "RU", "translations": {"ru": {"hello": "Привет"}}}
    options = (
        values
        if construction == "mapping"
        else RenderOptions(**values)
        if construction == "direct"
        else RenderOptions.create(**values)
    )
    template = document(paragraph("{d.n:formatN(1)}|{t(hello)}"))
    output = text(engine.render(template, {"n": 1234.5}, options))
    assert output == "1 234,5|Привет"
    if isinstance(options, RenderOptions):
        assert options.lang == "ru"


@pytest.mark.parametrize("characters", ["\ue001", "\ue000\ue001\ue002", "\U000f0001\U00100001"])
@pytest.mark.parametrize("path", ["plain", "string", "tree", "nested"])
def test_private_use_characters_survive_template_and_data_paths(engine: Engine, characters: str, path: str) -> None:
    if path == "plain":
        body = paragraph(characters + "{d.x}" + characters)
        data = {"x": characters}
        expected = characters * 3
    else:
        marker = "{d.groups[i].rows[j].x}" if path == "nested" else "{d.rows[i].x}"
        first = paragraph(characters + marker + characters)
        if path == "tree":
            first = (
                '<w:p><w:bookmarkStart w:id="1" w:name="item"/><w:r><w:t>'
                + escape(characters + marker + characters)
                + '</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>'
            )
        last = paragraph("{d.groups[i].rows[j+1].x}" if path == "nested" else "{d.rows[i+1].x}")
        body = first + last
        data = {"rows": [{"x": characters}, {"x": ""}]}
        expected = characters * 5
        if path == "nested":
            body = paragraph("{d.groups[i].name}") + body + paragraph("{d.groups[i+1].name}")
            data = {"groups": [{"name": "", "rows": data["rows"]}]}
    assert text(engine.render(document(body), data)) == expected


@pytest.mark.parametrize("construction", ["mapping", "direct", "create"])
@pytest.mark.parametrize("explicit_now", [False, True])
def test_reused_options_get_a_fresh_render_clock(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, construction: str, explicit_now: bool
) -> None:
    from datetime import UTC, datetime

    from carbotek.pipeline import document as pipeline

    values = {"complement": {"now": "EXPLICIT"} if explicit_now else {}}
    options = (
        values
        if construction == "mapping"
        else RenderOptions(**values)
        if construction == "direct"
        else RenderOptions.create(**values)
    )
    times = iter([datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)])

    class Clock:
        @staticmethod
        def now(zone: object) -> datetime:
            assert zone is UTC
            return next(times)

    monkeypatch.setattr(pipeline, "datetime", Clock)
    template = document(paragraph("{c.now}|{c.now}"))
    first = text(engine.render(template, {}, options))
    second = text(engine.render(template, {}, options))
    assert first == ("EXPLICIT|EXPLICIT" if explicit_now else "2026-01-01T00:00:00+00:00|2026-01-01T00:00:00+00:00")
    assert second == ("EXPLICIT|EXPLICIT" if explicit_now else "2026-01-02T00:00:00+00:00|2026-01-02T00:00:00+00:00")
    complement = options.complement if isinstance(options, RenderOptions) else options["complement"]
    assert dict(complement) == ({"now": "EXPLICIT"} if explicit_now else {})


@pytest.mark.parametrize("repeated", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_html_images_clear_source_descriptions_and_never_create_empty_media(
    engine: Engine, repeated: bool, empty: bool
) -> None:
    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    source = "data:image/png;base64," + ("" if empty else b64encode(stream.getvalue()).decode())
    html = '<p><img src="' + source + '"/></p>'
    body = paragraph("{d.rows[i].h:html}") + paragraph("{d.rows[i+1].h}") if repeated else paragraph("{d.h:html}")
    data = {"rows": [{"h": html}, {"h": html}]} if repeated else {"h": html}
    output = engine.render(document(body), data)
    drawings = list(root(output).iter(qn("wp:docPr")))
    assert len(drawings) == (2 if repeated else 1)
    assert all(node.get("descr") == "" for node in drawings)
    with ZipFile(BytesIO(output)) as archive:
        media = [archive.read(name) for name in archive.namelist() if name.startswith("word/media/")]
        assert media
        assert all(payload.startswith(b"\x89PNG\r\n\x1a\n") for payload in media)
        assert b"__CARBOTEK_HTML_IMAGE__" not in archive.read("word/document.xml")
        assert source.encode() not in archive.read("word/document.xml")
        if not empty:
            assert stream.getvalue() in media


@pytest.mark.parametrize("repeated", [False, True])
def test_html_remote_image_token_is_not_retained_in_description(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, repeated: bool
) -> None:
    from PIL import Image

    from carbotek.features import image

    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    source = "https://example.com/image.png?token=PRIVATE_IMAGE_TOKEN"
    requested: list[str] = []

    def fetch(urls, config):
        requested.extend(urls)
        return {url: (stream.getvalue(), "image/png") for url in urls}

    monkeypatch.setattr(image, "fetch_remote_images", fetch)
    html = '<p><img src="' + source + '"/></p>'
    body = paragraph("{d.rows[i].h:html}") + paragraph("{d.rows[i+1].h}") if repeated else paragraph("{d.h:html}")
    data = {"rows": [{"h": html}, {"h": html}]} if repeated else {"h": html}
    output = engine.render(document(body), data)
    assert source in requested
    assert all(node.get("descr") == "" for node in root(output).iter(qn("wp:docPr")))
    with ZipFile(BytesIO(output)) as archive:
        assert all(b"PRIVATE_IMAGE_TOKEN" not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("target_root", ["d", "c"])
@pytest.mark.parametrize("alias", ["none", "template", "options"])
def test_public_set_helpers_mutate_only_render_owned_data(engine: Engine, target_root: str, alias: str) -> None:
    cfg = {"x": "OLD", "nested": {"items": [1, 2]}}
    data = {"v": "NEW", "cfg": cfg}
    complement = {"cfg": {"x": "OLD", "nested": {"items": [1, 2]}}}
    expression = "d.v:_setInit(" + target_root + ".cfg):_setVal(x)"
    declaration = "{#update=" + expression + "}"
    update = "{" + expression + "}" if alias == "none" else "{$update}"
    options = {"complement": complement}
    if alias == "options":
        options["variable_str"] = declaration
    body = paragraph((declaration if alias == "template" else "") + update + "{" + target_root + ".cfg.x}")
    template = document(body)
    assert text(engine.render(template, data, options)) == "NEW"
    assert text(engine.render(template, data, options)) == "NEW"
    assert data == {"v": "NEW", "cfg": {"x": "OLD", "nested": {"items": [1, 2]}}}
    assert data["cfg"] is cfg
    assert complement == {"cfg": {"x": "OLD", "nested": {"items": [1, 2]}}}


@pytest.mark.parametrize("alias_source", ["options", "template", "header"])
@pytest.mark.parametrize("repeated", [False, True])
def test_color_alias_enables_postphase_in_plain_and_repeated_paragraphs(
    engine: Engine, alias_source: str, repeated: bool
) -> None:
    expression = "d.rows[i].color:color(p)" if repeated else "d.color:color(p)"
    declaration = "{#paint=" + expression + "}"
    body = (
        paragraph("{d.rows[i].name}{$paint}") + paragraph("{d.rows[i+1].name}")
        if repeated
        else paragraph("LABEL{$paint}")
    )
    options = {}
    parts = {}
    if alias_source == "options":
        options["variable_str"] = declaration
    elif alias_source == "header":
        parts["word/header1.xml"] = f"<w:hdr {_NAMESPACES}>" + paragraph(declaration) + "</w:hdr>"
    else:
        body = paragraph(declaration) + body
    data = (
        {"rows": [{"name": "A", "color": "FF0000"}, {"name": "B", "color": "00FF00"}]}
        if repeated
        else {"color": "FF0000"}
    )
    template = document(body, **parts)
    if alias_source == "options":
        with ZipFile(BytesIO(template)) as archive:
            assert b":color" not in archive.read("word/document.xml")
    output = root(engine.render(template, data, options))
    paragraphs = [node for node in output.findall("w:body/w:p", NAMESPACES) if "".join(node.itertext())]
    assert ["".join(node.itertext()) for node in paragraphs] == (["A", "B"] if repeated else ["LABEL"])
    assert [[color.get(qn("w:val")) for color in node.iter(qn("w:color"))] for node in paragraphs] == (
        [["ff0000"], ["00ff00"]] if repeated else [["ff0000"]]
    )


@pytest.mark.parametrize("literal", [" (collectively)", " (col)", " drop(col) keep(p)"])
def test_directive_like_literal_text_does_not_change_loop_render_path(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, literal: str
) -> None:
    original = DocxTextRenderer._render_element
    calls = []

    def record(self, element, *args, **kwargs):
        calls.append(element.tag)
        return original(self, element, *args, **kwargs)

    monkeypatch.setattr(DocxTextRenderer, "_render_element", record)
    body = paragraph("{d.rows[i].x}") + paragraph("{d.rows[i+1].x}")
    data = {"rows": [{"x": "{d.secret}"} for _ in range(50)], "secret": "LEAK"}
    baseline = engine.render(document(body), data)
    baseline_calls = list(calls)
    calls.clear()
    template = document(body + paragraph(literal))
    plan = engine.compile(template).plans["word/document.xml"]
    assert not plan.directives
    assert not plan.columns
    output = engine.render(template, data)
    assert calls == baseline_calls
    assert text(output) == text(baseline) + literal
    assert "LEAK" not in text(output)


@pytest.mark.parametrize("call", ["drop( col )", "drop('col')", 'drop("col")'])
def test_spaced_and_quoted_column_directives_remove_the_column(engine: Engine, call: str) -> None:
    body = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr><w:tc>"
        + paragraph("REMOVE {d.remove:" + call + "}")
        + "</w:tc><w:tc>"
        + paragraph("KEEP")
        + "</w:tc></w:tr></w:tbl>"
    )
    template = document(body)
    plan = engine.compile(template).plans["word/document.xml"]
    assert plan.directives and plan.columns
    output = root(engine.render(template, {"remove": True}))
    assert "".join(output.itertext()) == "KEEP"
    assert len(list(output.iter(qn("w:gridCol")))) == 1
    assert len(list(output.iter(qn("w:tc")))) == 1


@pytest.mark.parametrize("value", ["scalar", 7, True, False, 0, None])
@pytest.mark.parametrize("aliased", [False, True])
def test_scalar_child_without_formatters_renders_empty_across_paths(
    engine: Engine, value: object, aliased: bool
) -> None:
    plain = "{$child}" if aliased else "{d.a.b}"
    row = "{$rowchild}" if aliased else "{d.rows[i].a.b}"
    options = {"variable_str": "{#child=d.a.b}{#rowchild=d.rows[i].a.b}"} if aliased else {}
    template = document(
        paragraph("P[" + plain + "]")
        + paragraph("{d.rows[i].name}[" + row + "]")
        + paragraph("{d.rows[i+1].name}")
        + paragraph("H[{d.a.b:html(inline)}]")
    )
    data = {"a": value, "rows": [{"name": "A", "a": value}, {"name": "B", "a": value}]}
    assert text(engine.render(template, data, options)) == "P[]A[]B[]H[]"


@pytest.mark.parametrize(
    "part,tag",
    [
        ("word/header1.xml", "hdr"),
        ("word/footer1.xml", "ftr"),
        ("word/footnotes.xml", "footnotes"),
        ("word/endnotes.xml", "endnotes"),
        ("word/comments.xml", "comments"),
        ("customXml/item1.xml", None),
    ],
)
def test_template_option_grammar_applies_to_all_xml_parts_and_overrides_api(
    engine: Engine, part: str, tag: str | None
) -> None:
    declarations = "{o. lang = e n}{o.unknownOption=ignored}{o.preReleaseFeatureIn=4022008}TAIL"
    content = (
        f"<w:{tag} {_NAMESPACES}>" + paragraph(declarations) + f"</w:{tag}>"
        if tag
        else "<root>" + declarations + "</root>"
    )
    template = document(paragraph("{d.n:formatN(1)}"), **{part: content})
    compiled = engine.compile(template)
    assert dict(compiled.option_values) == {"lang": "en"}
    output = engine.render(template, {"n": 1234.5}, {"lang": "ru"})
    assert text(output) == "1,234.5"
    assert "".join(root(output, part).itertext()) == "TAIL"


@pytest.mark.parametrize(
    "declaration", ["{o.lang=EN}", "{o. lang = e n}", "{o.lang=en}{o.unknownOption=ignored}{o.styleSource=ignored}"]
)
def test_docx_common_option_grammar_and_style_source(engine: Engine, declaration: str) -> None:
    template = document(paragraph(declaration + "{d.n:formatN(1)}"))
    assert engine.compile(template).option_values["lang"] == "en"
    assert text(engine.render(template, {"n": 1234.5}, {"lang": "ru"})) == "1,234.5"


@pytest.mark.parametrize("reverse", [False, True])
def test_later_template_option_in_package_order_wins(engine: Engine, reverse: bool) -> None:
    parts = [
        ("word/header1.xml", f"<w:hdr {_NAMESPACES}>" + paragraph("{o.lang=en}") + "</w:hdr>"),
        ("word/footer1.xml", f"<w:ftr {_NAMESPACES}>" + paragraph("{o.lang=de}") + "</w:ftr>"),
    ]
    if reverse:
        parts.reverse()
    template = document(paragraph("{o.lang=ru}{d.n:formatN(1)}"), **dict(parts))
    assert engine.compile(template).option_values["lang"] == ("en" if reverse else "de")
    assert text(engine.render(template, {"n": 1234.5}, {"lang": "ru"})) == ("1,234.5" if reverse else "1.234,5")


@pytest.mark.parametrize(
    "part,tag",
    [(None, None), ("word/header1.xml", "hdr"), ("word/footer1.xml", "ftr"), ("word/endnotes.xml", "endnotes")],
)
def test_split_word_options_are_extracted_after_normalization(
    engine: Engine, part: str | None, tag: str | None
) -> None:
    split = "<w:p><w:r><w:t>{o.</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t> lang = e n}</w:t></w:r></w:p>"
    body = paragraph("{d.n:formatN(1)}")
    parts = {}
    if part is None:
        body = split + body
    else:
        parts[part] = f"<w:{tag} {_NAMESPACES}>" + split + f"</w:{tag}>"
    template = document(body, **parts)
    assert engine.compile(template).option_values["lang"] == "en"
    output = engine.render(template, {"n": 1234.5}, {"lang": "ru"})
    assert text(output) == "1,234.5"
    assert "{o." not in text(output, part or "word/document.xml")


def test_split_option_in_custom_xml_preserves_js_contract(engine: Engine) -> None:
    template = document(
        paragraph("{d.n:formatN(1)}"), **{"customXml/item1.xml": "<root><a>{o.</a><b> lang = e n}</b></root>"}
    )
    assert "lang" not in engine.compile(template).option_values
    output = engine.render(template, {"n": 1234.5}, {"lang": "ru"})
    assert text(output) == "1 234,5"
    assert text(output, "customXml/item1.xml") == "{o. lang = e n}"


@pytest.mark.parametrize("kind", ["plain", "html", "set"])
@pytest.mark.parametrize("value", ["paid", "unpaid"])
@pytest.mark.parametrize("call", ["show", "show()"])
def test_show_without_argument_matches_js_and_never_prints_a_sentinel(
    engine: Engine, kind: str, value: str, call: str
) -> None:
    suffix = ":html" if kind == "html" else ":set(c.label)" if kind == "set" else ""
    body = paragraph("[{d.x:ifEQ('paid'):" + call + suffix + "}]")
    if kind == "set":
        body += paragraph("[{c.label}]")
    expected = (
        ("[][]" if value == "paid" else "[][unpaid]") if kind == "set" else ("[]" if value == "paid" else "[unpaid]")
    )
    assert text(engine.render(document(body), {"x": value})) == expected


@pytest.mark.parametrize("value,expected", [("paid", "Yes"), ("unpaid", "unpaid")])
def test_set_receives_the_conditional_show_pipeline_result(engine: Engine, value: str, expected: str) -> None:
    template = document(paragraph("{d.x:ifEQ('paid'):show('Yes'):set(c.label)}") + paragraph("{c.label}"))
    data = {"x": value}
    assert text(engine.render(template, data)) == expected
    assert data == {"x": value}


@pytest.mark.parametrize("marker_root", ["d", "c"])
def test_color_and_drawing_color_attribute_do_not_require_bindcolor(engine: Engine, marker_root: str) -> None:
    body = (
        paragraph("LABEL{" + marker_root + ".tint:color(p)}")
        + '<w:p><w:r><w:drawing><a:solidFill><a:srgbClr val="{'
        + marker_root
        + '.tint:colorToDocx}"/></a:solidFill></w:drawing></w:r></w:p>'
    )
    template = document(body)
    with ZipFile(BytesIO(template)) as archive:
        assert b"bindColor" not in archive.read("word/document.xml")
    output = root(
        engine.render(template, {"tint": "#FF0000"} if marker_root == "d" else {}, {"complement": {"tint": "#FF0000"}})
    )
    assert "".join(output.itertext()) == "LABEL"
    assert [node.get(qn("w:val")) for node in output.iter(qn("w:color"))] == ["ff0000"]
    assert [node.get("val") for node in output.iter(qn("a:srgbClr"))] == ["FF0000"]


@pytest.mark.parametrize("target_root", ["d", "c"])
@pytest.mark.parametrize("set_part", ["body", "footer"])
def test_document_sets_feed_image_hyperlink_color_and_html_phases(
    engine: Engine, target_root: str, set_part: str
) -> None:
    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", (3, 2), "red").save(stream, format="PNG")
    data = {
        "imageSource": "data:image/png;base64," + b64encode(stream.getvalue()).decode(),
        "urlSource": "https://example.com/fresh",
        "colorSource": "FF0000",
        "htmlSource": "<b>READY</b>",
    }
    original = dict(data)
    declarations = "".join(
        "{d." + source + ":set(" + target_root + "." + target + ")}"
        for source, target in [
            ("imageSource", "picture"),
            ("urlSource", "path"),
            ("colorSource", "tint"),
            ("htmlSource", "content"),
        ]
    )
    body = (
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="300000" cy="200000"/>'
        '<wp:docPr id="1" name="image" descr="{' + target_root + '.picture}"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdPlaceholder"/>'
        "</pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        + paragraph("LABEL{" + target_root + ".tint:color(p)}")
        + paragraph("{" + target_root + ".content:html}")
        + '<w:p><w:hyperlink r:id="rId1"><w:r><w:t>link</w:t></w:r></w:hyperlink></w:p>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="{'
        + target_root
        + '.path}" TargetMode="External" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/></Relationships>'
    )
    parts = {"word/_rels/document.xml.rels": relationships}
    if set_part == "body":
        body = paragraph(declarations) + body
    else:
        parts["word/footer1.xml"] = f"<w:ftr {_NAMESPACES}>" + paragraph(declarations) + "</w:ftr>"
    complement = {"picture": "", "path": "https://example.com/stale", "tint": "000000", "content": "STALE"}
    output = engine.render(document(body, **parts), data, {"complement": complement})
    assert text(output) == "LABELREADYlink"
    assert [node.get(qn("w:val")) for node in root(output).iter(qn("w:color"))] == ["ff0000"]
    assert root(output, "word/_rels/document.xml.rels")[0].get("Target") == "https://example.com/fresh"
    with ZipFile(BytesIO(output)) as archive:
        assert stream.getvalue() in [
            archive.read(name) for name in archive.namelist() if name.startswith("word/media/")
        ]
    assert data == original
    assert complement == {"picture": "", "path": "https://example.com/stale", "tint": "000000", "content": "STALE"}


@pytest.mark.parametrize("target_root", ["d", "c"])
@pytest.mark.parametrize("set_part", ["body", "footer"])
def test_document_sets_feed_native_chart_loop_data(engine: Engine, target_root: str, set_part: str) -> None:
    from copy import deepcopy

    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    chart = f'<c:chartSpace xmlns:c="{chart_ns}"><c:chart><c:plotArea><c:barChart><c:ser><c:val><c:numRef><c:f>Sheet1!$A$1:$A$2</c:f><c:numCache><c:ptCount val="2"/><c:pt idx="0"><c:v>{{d.groups[i].values[i].n}}</c:v></c:pt><c:pt idx="1"><c:v>{{d.groups[i].values[i+1].n}}</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>'
    chart = chart.replace("{d.", "{" + target_root + ".")
    declarations = "{d.fresh:set(" + target_root + ".groups)}"
    body = (
        paragraph("{" + target_root + ".groups[i].name}")
        + f'<w:p><w:r><w:drawing><c:chart xmlns:c="{chart_ns}" r:id="rIdChart"/></w:drawing></w:r></w:p>'
        + paragraph("{" + target_root + ".groups[i+1].name}")
    )
    parts = {
        "word/charts/chart1.xml": chart,
        "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdChart" Target="charts/chart1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"/></Relationships>',
    }
    if set_part == "body":
        body = paragraph(declarations) + body
    else:
        parts["word/footer1.xml"] = f"<w:ftr {_NAMESPACES}>" + paragraph(declarations) + "</w:ftr>"
    stale = [{"name": "STALE", "values": [{"n": 999}]}]
    data = {
        "fresh": [{"name": "A", "values": [{"n": 11}, {"n": 12}]}, {"name": "B", "values": [{"n": 21}]}],
        "groups": deepcopy(stale),
    }
    snapshot = deepcopy(data)
    complement = {"groups": deepcopy(stale)}
    output = engine.render(document(body, **parts), data, {"complement": complement})
    assert text(output) == "AB"
    with ZipFile(BytesIO(output)) as archive:
        charts = [
            etree.fromstring(archive.read(name))
            for name in archive.namelist()
            if "CarbotekChart" in name and name.endswith(".xml")
        ]
        assert "word/charts/chart1.xml" not in archive.namelist()
    assert [[node.text for node in item.iter(f"{{{chart_ns}}}v")] for item in charts] == [["11", "12"], ["21"]]
    assert data == snapshot
    assert complement == {"groups": stale}


@pytest.mark.parametrize("rows", [False, True])
@pytest.mark.parametrize("tree", [False, True])
def test_bindcolor_is_independent_of_loop_container_and_bookmarks(engine: Engine, rows: bool, tree: bool) -> None:
    first = paragraph("{d.rows[i].x}", properties='<w:rPr><w:color w:val="123456"/></w:rPr>')
    if tree:
        first = first.replace("<w:p>", '<w:p><w:bookmarkStart w:id="1" w:name="item"/>').replace(
            "</w:p>", '<w:bookmarkEnd w:id="1"/></w:p>'
        )
    last = paragraph("{d.rows[i+1].x}")
    loop = (
        "<w:tbl><w:tblGrid/><w:tr><w:tc>" + first + "</w:tc></w:tr><w:tr><w:tc>" + last + "</w:tc></w:tr></w:tbl>"
        if rows
        else first + last
    )
    template = document(paragraph("{bindColor(123456,hexa)=d.rows[i].c}") + loop)
    output = root(engine.render(template, {"rows": [{"x": "A", "c": "ff0000"}, {"x": "B", "c": "00ff00"}]}))
    assert "".join(output.itertext()) == "AB"
    assert [node.get(qn("w:val")) for node in output.iter(qn("w:color"))] == ["ff0000", "00ff00"]


@pytest.mark.parametrize("tree", [False, True])
def test_hidden_paragraph_loop_inside_cell_keeps_a_paragraph(engine: Engine, tree: bool) -> None:
    first = paragraph("{d.rows[i].show:showBegin}")
    if tree:
        first = first.replace("<w:p>", '<w:p><w:bookmarkStart w:id="1" w:name="item"/>').replace(
            "</w:p>", '<w:bookmarkEnd w:id="1"/></w:p>'
        )
    body = (
        "<w:tbl><w:tblGrid/><w:tr><w:tc>"
        + first
        + paragraph("HIDDEN")
        + paragraph("{d.rows[i].show:showEnd}")
        + paragraph("{d.rows[i+1].show}")
        + "</w:tc></w:tr></w:tbl>"
    )
    output = root(engine.render(document(body), {"rows": [{"show": False}, {"show": False}]}))
    assert "HIDDEN" not in "".join(output.itertext())
    cells = list(output.iter(qn("w:tc")))
    assert len(cells) == 1
    assert cells[0].find(qn("w:p")) is not None


@pytest.mark.parametrize("inline", [False, True])
def test_standalone_html_does_not_evaluate_markers_from_generated_content(inline: bool) -> None:
    from carbotek.features.html import HtmlSubstitution
    from carbotek.opc.repository import OpcTemplateRepository

    repository = OpcTemplateRepository()
    marker = "{d.h:html(inline)}" if inline else "{d.h:html}"
    package = repository.load(document(paragraph(marker)))
    output = HtmlSubstitution().apply(
        package, {"h": "<p>{c.apiKey:html(inline)}</p>", "secret": "LEAK"}, RenderOptions(complement={"apiKey": "LEAK"})
    )
    assert text(repository.dump(output)) == "{c.apiKey:html(inline)}"


@pytest.mark.parametrize("before", [False, True])
def test_standalone_html_keeps_template_inline_markers_but_not_identical_data_markers(before: bool) -> None:
    from carbotek.features.html import HtmlSubstitution
    from carbotek.opc.repository import OpcTemplateRepository

    inline = "{d.inline:html(inline)}"
    marker = inline + "|{d.block:html}" if before else "{d.block:html}|" + inline
    repository = OpcTemplateRepository()
    package = repository.load(document(paragraph(marker)))
    output = HtmlSubstitution().apply(
        package, {"inline": "<b>TEMPLATE</b>", "block": "<p>" + inline + "</p>"}, RenderOptions()
    )
    assert text(repository.dump(output)) == ("TEMPLATE|" + inline if before else inline + "|TEMPLATE")


@pytest.mark.parametrize("feature", ["color", "hyperlink", "image"])
def test_standalone_features_do_not_resolve_marker_looking_source_values(
    feature: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carbotek.features import ColorBinding, HyperlinkSubstitution, ImageSubstitution
    from carbotek.opc.repository import OpcTemplateRepository

    repository = OpcTemplateRepository()
    if feature == "color":
        package = repository.load(document(paragraph("LABEL{d.value:color(p)}")))
        output = ColorBinding().apply(package, {"value": "{d.secret:color(p)}", "secret": "FF0000"}, RenderOptions())
        assert [node.get(qn("w:val")) for node in root(repository.dump(output)).iter(qn("w:color"))] == ["888888"]
    elif feature == "hyperlink":
        body = '<w:p><w:hyperlink r:id="{d.value}"><w:r><w:t>LABEL</w:t></w:r></w:hyperlink></w:p>'
        relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        package = repository.load(document(body, **{"word/_rels/document.xml.rels": relationships}))
        output = HyperlinkSubstitution().apply(package, {"value": "{d.secret}", "secret": "https://example.com/LEAK"})
    else:
        from carbotek.features import image

        def no_remote_sources(urls, config):
            assert not urls
            return {}

        monkeypatch.setattr(image, "fetch_remote_images", no_remote_sources)
        body = '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1000" cy="1000"/><wp:docPr id="1" name="image" descr="{d.value}"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdPlaceholder"/></pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
        package = repository.load(document(body))
        output = ImageSubstitution().apply(package, {"value": "{d.secret}", "secret": "https://example.com/LEAK"})
    with ZipFile(BytesIO(repository.dump(output))) as archive:
        assert all(b"LEAK" not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("control", ["\x01", "\x02", "\x08", "\x0e", "\x1f"])
def test_html_image_control_characters_have_a_typed_error(engine: Engine, control: str) -> None:
    from carbotek.errors import MarkerResolutionError

    template = document(paragraph("{d.h:html}"))
    with pytest.raises(MarkerResolutionError, match="Invalid HTML image source") as failure:
        engine.render(template, {"h": '<img src="data:image/png;base64,AAA' + control + 'BBB"/>'})
    assert failure.value.code == "TEMPLATE_ERROR"
    assert isinstance(failure.value.__cause__, ValueError)


@pytest.mark.parametrize("control", ["\x01", "\x02", "\x0e"])
@pytest.mark.parametrize("kind", ["html", "relationship"])
def test_hyperlink_control_characters_never_escape_as_lxml_errors(engine: Engine, control: str, kind: str) -> None:
    url = "https://example.com/a" + control + "token"
    if kind == "html":
        template = document(paragraph("{d.h:html}"))
        data = {"h": '<a href="' + url + '">OK</a>'}
    else:
        body = '<w:p><w:hyperlink r:id="rId1"><w:r><w:t>OK</w:t></w:r></w:hyperlink></w:p>'
        rel = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="{d.url}" TargetMode="External" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/></Relationships>'
        template = document(body, **{"word/_rels/document.xml.rels": rel})
        data = {"url": url}
    output = engine.render(template, data)
    assert text(output) == "OK"
    with ZipFile(BytesIO(output)) as archive:
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                etree.fromstring(archive.read(name))


@pytest.mark.parametrize(
    "formatter,value,expected",
    [
        ("formatC", 42.5, "42.50 $"),
        ("formatC", "", "0.00 $"),
        ("formatC", "1_000", "NaN $"),
        ("formatN", 42.5, "NaN"),
        ("formatN", "", "NaN"),
        ("formatN", "1_000", "NaN"),
    ],
)
def test_explicit_empty_numeric_formatter_argument_matches_js(
    engine: Engine, formatter: str, value: object, expected: str
) -> None:
    template = document(paragraph("{d.x:" + formatter + "('')}"))
    assert (
        text(engine.render(template, {"x": value}, {"lang": "en", "currency": {"source": "USD", "target": "USD"}}))
        == expected
    )


@pytest.mark.parametrize("picture", [False, True])
def test_compiled_template_does_not_repeat_formatter_validation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, picture: bool
) -> None:
    from PIL import Image

    calls = []
    original = DocxTextRenderer._validate_formatters

    def record(self, tree):
        calls.append(tree.tag)
        return original(self, tree)

    monkeypatch.setattr(DocxTextRenderer, "_validate_formatters", record)
    template = document(paragraph("{d.value}"))
    engine.compile(template)
    assert len(calls) == 1
    value = "TEXT"
    if picture:
        stream = BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
        value = "data:image/png;base64," + b64encode(stream.getvalue()).decode()
    for _ in range(2):
        output = engine.render(template, {"value": value})
        assert text(output) == value
    assert len(calls) == 1


def test_repeated_qr_images_are_generated_once_per_render(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from carbotek.features import barcode

    original = barcode.segno.make
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(barcode.segno, "make", record)
    drawing = '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="100000" cy="100000"/><wp:docPr id="1" name="QR" descr="{d.rows[i].code:barcode(qrcode)}"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdPlaceholder"/></pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    template = document(paragraph("{d.rows[i].code}") + drawing + paragraph("{d.rows[i+1].code}"))
    data = {"rows": [{"code": "SAME QR PAYLOAD"} for _ in range(300)]}
    for render_index in range(2):
        output = engine.render(template, data)
        assert len(calls) == render_index + 1
        assert len(list(root(output).iter(qn("wp:docPr")))) == 300
        with ZipFile(BytesIO(output)) as archive:
            media = [archive.read(name) for name in archive.namelist() if name.startswith("word/media/")]
            assert len(media) == 1
            assert media[0].startswith(b"\x89PNG")


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_inside_loop_span_removes_nested_xml_branch(engine: Engine, overlap: bool, first: bool, second: bool) -> None:
    start = "{d.rows[i].x}" + ("{d.rows[i].remove:drop(p,3)}" if overlap else "")
    condition = "other" if overlap else "remove"
    template = document(
        paragraph(start)
        + paragraph("{d.rows[i]." + condition + ":drop(p,2)}MIDDLE")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    rows = [
        {"x": "A", "remove": first, "other": second},
        {"x": "B", "remove": second, "other": first},
    ]
    assert text(engine.render(template, {"rows": rows})) == ("AFTER" if overlap and first else "ABAFTER")


@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_inside_loop_span_extending_after_loop_removes_only_nested_branch(
    engine: Engine, action: str, first: bool, second: bool
) -> None:
    template = document(
        paragraph("{d.rows[i].x}")
        + paragraph("MIDDLE{d.rows[i].remove:" + action + "(p,3)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("TAIL")
        + paragraph("AFTER")
    )
    assert (
        text(engine.render(template, {"rows": [{"x": "A", "remove": first}, {"x": "B", "remove": second}]}))
        == "ABAFTER"
    )


@pytest.mark.parametrize("same_end", [False, True])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_removed_inner_span_endpoint_preserves_outer_span_end(
    engine: Engine, same_end: bool, first: bool, second: bool
) -> None:
    count = 4 if same_end else 5
    template = document(
        paragraph("{d.rows[i].x}{d.rows[i].remove:drop(p," + str(count) + ")}")
        + paragraph("MIDDLE{d.rows[i].other:drop(p,3)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("TAIL")
        + paragraph("LAST")
        + paragraph("AFTER")
    )
    rows = [
        {"x": "A", "remove": first, "other": second},
        {"x": "B", "remove": second, "other": first},
    ]
    expected = "LASTAFTER" if same_end else "AFTER"
    if not first:
        expected = "ABLASTAFTER"
    assert text(engine.render(template, {"rows": rows})) == expected


@pytest.mark.parametrize("marker_root", ["d", "c"])
@pytest.mark.parametrize(
    "values,path,classification",
    [
        ({}, "m", "U"),
        ({"m": None}, "m", "N"),
        ({"m": None}, "m.child", "U"),
        ({"m": 7}, "m.child", "U"),
        ({"items": [None]}, "items[0]", "N"),
        ({"items": [None]}, "items[0].child", "U"),
        ({"items": [{"id": "A", "value": None}]}, "items[id=A].value", "N"),
        ({"items": [{"id": "A", "value": None}]}, "items[id=A].value.child", "U"),
    ],
)
def test_parent_selector_missing_and_null_match_shared_evaluator(
    engine: Engine, marker_root: str, values: dict, path: str, classification: str
) -> None:
    location = marker_root + ".rows[i].." + path
    check = "{" + location + ":ifEQ(undefined):show('U'):elseShow('N')}"
    inverse = "{" + location + ":ifNE(undefined):show('N'):elseShow('U')}"
    html = "{" + location + ":ifEQ(undefined):show('U'):elseShow('N'):html(inline)}"
    template = document(
        paragraph("{" + marker_root + ".rows[i].x}" + check + inverse + html)
        + paragraph("{" + marker_root + ".rows[i+1].x}")
    )
    data = {**values, "rows": [{"x": "A"}, {"x": "B"}]}
    output = engine.render(template, data) if marker_root == "d" else engine.render(template, {}, {"complement": data})
    assert text(output) == "A" + classification * 3 + "B" + classification * 3


def test_finished_render_sessions_are_released_without_cyclic_gc(engine: Engine) -> None:
    import gc

    from carbotek.docx.session import RenderSession

    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        before = sum(isinstance(item, RenderSession) for item in gc.get_objects())
        template = document(paragraph("{d.value:html(inline)}"))
        for _ in range(5):
            assert text(engine.render(template, {"value": "<b>READY</b>"})) == "READY"
        after = sum(isinstance(item, RenderSession) for item in gc.get_objects())
        assert after == before
    finally:
        if enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_internal_span_removes_html_branch_before_feature_render(engine: Engine, first: bool, second: bool) -> None:
    template = document(
        paragraph("{d.rows[i].x}")
        + paragraph("MIDDLE{d.rows[i].remove:drop(p,2)}{d.rows[i].html:html(inline)}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("AFTER")
    )
    rows = [
        {"x": "A", "remove": first, "html": "<b>HTML</b>"},
        {"x": "B", "remove": second, "html": "<b>HTML</b>"},
    ]
    assert text(engine.render(template, {"rows": rows})) == "ABAFTER"


@pytest.mark.parametrize(
    "chain,expected",
    [
        ("padl('8,0)", "00ABCDEF"),
        ("padr('8,0)", "ABCDEF00"),
        ("substr('1,3)", "BC"),
        ("replace('A,B)", "BBCDEF"),
        ("substr('1,2',3)", "ABC"),
        ("replace('A,B','X)", "ABCDEF"),
    ],
)
def test_multiargument_unclosed_quotes_follow_js_across_render_paths(engine: Engine, chain: str, expected: str) -> None:
    template = document(
        paragraph("{d.s:" + chain + "}")
        + paragraph("{d.rows[i].x}{d.rows[i].s:" + chain + "}")
        + paragraph("{d.rows[i+1].x}")
        + paragraph("{d.s:" + chain + ":html(inline)}")
    )
    data = {"s": "ABCDEF", "rows": [{"x": "A", "s": "ABCDEF"}, {"x": "B", "s": "ABCDEF"}]}
    html_expected = expected if chain in {"substr('1,2',3)", "replace('A,B','X)"} else "ABCDEF"
    assert text(engine.render(template, data)) == expected + "A" + expected + "B" + expected + html_expected


@pytest.mark.parametrize("chain,prepend", [("append('X)", False), ("prepend('X)", True)])
@pytest.mark.parametrize("paragraph_style", [False, True])
def test_malformed_html_helper_arguments_follow_js_preprocessing(
    engine: Engine, chain: str, prepend: bool, paragraph_style: bool
) -> None:
    body = paragraph("{d.s:" + chain + ":html(inline)}")
    if paragraph_style:
        body = body.replace("<w:p>", '<w:p><w:pPr><w:pStyle w:val="Style1"/></w:pPr>')
    suffix = "X):getHTMLContentDocx('document.xml',','inline"
    expected = suffix + "ABCDEF" if prepend else "ABCDEF" + suffix
    assert text(engine.render(document(body), {"s": "ABCDEF"})) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"a": 1}, "[object Object]"),
        ([1, 2, None], "1,2,"),
        ([None, True, False, {}, ["A", None, "B"]], ",true,false,[object Object],A,,B"),
        ([], ""),
        ({"nested": [1]}, "[object Object]"),
        (["{d.secret}", None], "{d.secret},"),
    ],
)
def test_container_values_use_js_strings_and_remain_literal_in_loops(
    engine: Engine, value: object, expected: str
) -> None:
    template = document(paragraph("{d.v}") + paragraph("{d.rows[i].x}{d.rows[i].v}") + paragraph("{d.rows[i+1].x}"))
    data = {"v": value, "secret": "LEAK", "rows": [{"x": "A", "v": value}, {"x": "B", "v": value}]}
    assert text(engine.render(template, data)) == expected + "A" + expected + "B" + expected


@pytest.mark.parametrize("shape", ["sorted-filter", "sorted-repeat", "sorted-filter-repeat"])
@pytest.mark.parametrize("action", ["drop", "keep"])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_crossing_combined_selectors_preserve_input_control_and_sorted_repeats(
    engine: Engine, shape: str, action: str, first: bool, second: bool
) -> None:
    begin = {
        "sorted-filter": "ord,active=true",
        "sorted-repeat": "ord*qty",
        "sorted-filter-repeat": "ord*qty,active=true",
    }[shape]
    end = begin.replace("ord", "ord+1", 1)
    template = document(
        paragraph("{d.rows[" + begin + "].x}{d.rows[" + begin + "].remove:" + action + "(p,2)}")
        + paragraph("{d.rows[" + end + "].x}")
        + paragraph("AFTER")
    )
    rows = [
        {"x": "X", "ord": 0, "active": False, "qty": 2, "remove": True},
        {"x": "B", "ord": 2, "active": True, "qty": 2, "remove": first},
        {"x": "A", "ord": 1, "active": True, "qty": 1, "remove": second},
    ]
    condition = True if shape == "sorted-repeat" else first
    visible = condition if action == "keep" else not condition
    value = {"sorted-filter": "AB", "sorted-repeat": "XXABB", "sorted-filter-repeat": "ABB"}[shape]
    assert text(engine.render(template, {"rows": rows})) == (value if visible else "") + "AFTER"


@pytest.mark.parametrize("shape", ["html", "static-attribute", "iterator-attribute"])
@pytest.mark.parametrize("first,second", [(False, False), (False, True), (True, False), (True, True)])
def test_coincident_crossing_spans_remove_deferred_and_attribute_branches(
    engine: Engine, shape: str, first: bool, second: bool
) -> None:
    value = "{d.rows[i].html:html(inline)}" if shape == "html" else "{d.rows[i].x}"
    opening = paragraph(value + "{d.rows[i].a:drop(p,2)}{d.rows[i].b:drop(p,2)}")
    if shape != "html":
        attribute = "{d.label}" if shape == "static-attribute" else "{d.rows[i].x}"
        opening = opening.replace("<w:r>", '<w:r w:rsidR="' + attribute + '">')
    template = document(opening + paragraph("{d.rows[i+1].x}") + paragraph("AFTER"))
    rows = [
        {"x": "A", "html": "<b>HTML</b>", "a": first, "b": second},
        {"x": "B", "html": "<b>HTML</b>", "a": not first, "b": not second},
    ]
    assert text(engine.render(template, {"label": "11223344", "rows": rows})) == "AFTER"


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_coincident_spans_inside_nested_loop_with_distinct_sort_keys(engine: Engine, empty: bool, hidden: bool) -> None:
    template = document(
        paragraph("{d.rows[i].items[j].x}{d.rows[i].a:drop(p,2)}{d.rows[i].b:drop(p,2)}")
        + paragraph("{d.rows[i].items[j+1].x}")
        + paragraph("{d.rows[i+1].items[j].x}")
        + paragraph("AFTER")
    )
    rows = [
        {"a": hidden, "b": False, "items": [] if empty else [{"x": "A", "j": 0}, {"x": "B", "j": 1}]},
        {"a": False, "b": True, "items": [{"x": "C", "j": 0}]},
    ]
    expected = "AFTER" if empty or hidden else "ABAFTER"
    assert text(engine.render(template, {"rows": rows})) == expected
