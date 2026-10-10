"""Template syntax follows Carbone 5.14.4, including the errors for forms it rejects."""

from base64 import b64encode
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image
from test_render_session import document, paragraph, text

from carbotek import Engine
from carbotek.errors import MarkerResolutionError
from carbotek.formatters.registry import UnknownFormatterError


def _row(content: str) -> str:
    return f"<w:tr><w:tc>{paragraph(content)}</w:tc></w:tr>"


def _table(*rows: str) -> str:
    return "<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid>" + "".join(rows) + "</w:tbl>"


def _png_uri() -> str:
    stream = BytesIO()
    Image.new("RGB", (4, 4), "red").save(stream, format="PNG")
    return "data:image/png;base64," + b64encode(stream.getvalue()).decode()


@pytest.fixture(params=[False, True], ids=["native", "python"])
def engine(request: pytest.FixtureRequest) -> Engine:
    if request.param:
        request.getfixturevalue("python_only")
    return Engine()


def test_condition_after_directive_is_an_unknown_formatter(engine: Engine) -> None:
    template = document(paragraph("PRICE {d.price:keep(p):ifGT(0)}"))
    with pytest.raises(UnknownFormatterError, match='Formatter "keep" does not exist'):
        engine.render(template, {"price": 5})


@pytest.mark.parametrize(("price", "expected"), [(0, "BEFOREAFTER"), (5, "BEFOREPRICE AFTER")])
def test_condition_before_directive(engine: Engine, price: int, expected: str) -> None:
    body = paragraph("BEFORE") + paragraph("PRICE {d.price:ifGT(0):keep(p)}") + paragraph("AFTER")
    assert text(engine.render(document(body), {"price": price})) == expected


@pytest.mark.parametrize(("target", "closest"), [("tr", "p"), ("tbl", "col"), ("xyz", "p")])
def test_unknown_directive_target_is_an_error(engine: Engine, target: str, closest: str) -> None:
    template = document(_table(_row("R1 {d.x:ifEM:drop(" + target + ")}"), _row("R2")))
    with pytest.raises(MarkerResolutionError, match=rf'Do you mean ":drop\({closest}\)"'):
        engine.render(template, {"x": ""})


def test_row_and_table_directives(engine: Engine) -> None:
    rows = document(_table(_row("R1 {d.x:ifEM:drop(row)}"), _row("R2")))
    assert text(engine.render(rows, {"x": ""})) == "R2"
    table = document(paragraph("BEFORE") + _table(_row("R1 {d.x:ifEM:drop(table)}")) + paragraph("AFTER"))
    assert text(engine.render(table, {"x": ""})) == "BEFOREAFTER"


@pytest.mark.parametrize("loop", [False, True], ids=["no-loop", "after-loop"])
def test_iterator_aggregate_outside_its_loop_is_an_error(engine: Engine, loop: bool) -> None:
    body = paragraph("{d.rows[i].v}") + paragraph("{d.rows[i+1].v}") if loop else ""
    template = document(body + paragraph("SUM {d.rows[i].v:aggSum}"))
    with pytest.raises(MarkerResolutionError, match=r'has no corresponding \[i\+1\] for array "rows"'):
        engine.render(template, {"rows": [{"v": 1}, {"v": 2}]})


def test_array_aggregate_outside_a_loop(engine: Engine) -> None:
    body = paragraph("{d.rows[i].v}") + paragraph("{d.rows[i+1].v}") + paragraph("SUM {d.rows[].v:aggSum}")
    assert text(engine.render(document(body), {"rows": [{"v": 1}, {"v": 2}]})) == "12SUM 3"


def test_image_data_in_text_stays_text(engine: Engine) -> None:
    uri = _png_uri()
    output = engine.render(document(paragraph("{d.logo}")), {"logo": uri})
    assert text(output) == uri
    with ZipFile(BytesIO(output)) as archive:
        assert not [name for name in archive.namelist() if name.startswith("word/media/")]


def test_size_suffix_in_picture_description_is_not_a_size(engine: Engine) -> None:
    picture = (
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/>'
        '<wp:docPr id="1" name="Picture" descr="{d.img}:100*50"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdTemplate"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    output = engine.render(document(picture), {"img": _png_uri()})
    with ZipFile(BytesIO(output)) as archive:
        xml = archive.read("word/document.xml").decode()
    assert 'cx="952500" cy="952500"' in xml
