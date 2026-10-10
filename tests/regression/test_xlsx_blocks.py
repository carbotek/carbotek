"""Worksheet blocks must not hide template nodes outside loop copies."""

from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from lxml import etree

from carbotek import Engine

_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _template() -> bytes:
    values = [
        "{d.title}",
        "{d.show:showBegin}VISIBLE",
        "{d.show:showEnd}",
        "{d.rows[i].value}",
        "{d.rows[i+1].value}",
        "Total {d.total}",
    ]
    rows = "".join(
        f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>'
        for index, value in enumerate(values, 1)
    )
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" Target="xl/workbook.xml" Type="{_REL}/officeDocument"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{_SHEET}" xmlns:r="{_REL}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rIdSheet"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG}"><Relationship Id="rIdSheet" Target="worksheets/sheet1.xml" Type="{_REL}/worksheet"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SHEET}"><sheetData>{rows}</sheetData></worksheet>',
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("python", [False, True], ids=["native", "python"])
@pytest.mark.parametrize("show", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_sheet_loop_and_block_render_fields_outside_copies(
    request: pytest.FixtureRequest, python: bool, show: bool, empty: bool
) -> None:
    if python:
        request.getfixturevalue("python_only")
    output = Engine().render(
        _template(),
        {"title": "TITLE", "total": 42, "show": show, "rows": [] if empty else [{"value": "A"}, {"value": "B"}]},
    )
    with ZipFile(BytesIO(output)) as archive:
        sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    values = [node.text for node in sheet.iter(f"{{{_SHEET}}}t") if node.text]
    assert values == ["TITLE", *(["VISIBLE"] if show else []), *([] if empty else ["A", "B"]), "Total 42"]
    assert all("{" not in value for value in values)


@pytest.mark.parametrize("command", ["render", "bench", "markers"])
def test_cli_workbook_content_overrides_docx_filename(tmp_path, capsys, command: str) -> None:
    from carbotek.cli import main

    template = tmp_path / "misnamed.docx"
    template.write_bytes(_template())
    data = tmp_path / "data.json"
    data.write_text('{"title":"TITLE","show":true,"rows":[{"value":"A"}],"total":42}', encoding="utf-8")
    arguments = [command, str(template)]
    if command != "markers":
        arguments.append(str(data))
    if command == "bench":
        arguments.extend(["-n", "1"])
    assert main(arguments) == 0
    captured = capsys.readouterr()
    if command == "render":
        output = tmp_path / "misnamed.out.xlsx"
        assert output.exists()
        with ZipFile(output) as archive:
            sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert [node.text for node in sheet.iter(f"{{{_SHEET}}}t") if node.text] == [
            "TITLE",
            "VISIBLE",
            "A",
            "Total 42",
        ]
    elif command == "bench":
        assert captured.out.startswith("misnamed.docx: xlsx, n=1")
    else:
        assert "xl/worksheets/sheet1.xml\t{d.title}" in captured.out


@pytest.mark.parametrize("python", [False, True], ids=["native", "python"])
@pytest.mark.parametrize("show", [False, True])
@pytest.mark.parametrize("leaf", ["v", "f", "oddHeader"])
def test_sheet_block_and_loop_reach_other_text_leaf_kinds(
    request: pytest.FixtureRequest, python: bool, show: bool, leaf: str
) -> None:
    if python:
        request.getfixturevalue("python_only")
    stream = BytesIO()
    with ZipFile(BytesIO(_template())) as source, ZipFile(stream, "w") as destination:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                sheet = etree.fromstring(content)
                if leaf == "oddHeader":
                    parent = etree.SubElement(sheet, f"{{{_SHEET}}}headerFooter")
                else:
                    row = etree.SubElement(sheet.find(f"{{{_SHEET}}}sheetData"), f"{{{_SHEET}}}row", r="7")
                    parent = etree.SubElement(row, f"{{{_SHEET}}}c", r="A7")
                etree.SubElement(parent, f"{{{_SHEET}}}{leaf}").text = "{d.total}"
                content = etree.tostring(sheet)
            destination.writestr(name, content)
    output = Engine().render(
        stream.getvalue(), {"title": "TITLE", "total": 42, "show": show, "rows": [{"value": "A"}, {"value": "B"}]}
    )
    with ZipFile(BytesIO(output)) as archive:
        sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    assert [node.text for node in sheet.iter(f"{{{_SHEET}}}{leaf}")] == ["42"]
    assert all("{" not in (node.text or "") for node in sheet.iter())


@pytest.mark.parametrize("python", [False, True], ids=["native", "python"])
def test_split_workbook_option_preserves_js_preprocessor_contract(request: pytest.FixtureRequest, python: bool) -> None:
    if python:
        request.getfixturevalue("python_only")
    stream = BytesIO()
    with ZipFile(BytesIO(_template())) as source, ZipFile(stream, "w") as destination:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = content.replace(
                    b"<t>{d.title}</t>", b"<r><t>{o.</t></r><r><t> lang = e n}</t></r><r><t>{d.title}</t></r>"
                )
            destination.writestr(name, content)
    engine = Engine()
    assert "lang" not in engine.compile(stream.getvalue()).option_values
    output = engine.render(stream.getvalue(), {"title": "TITLE", "total": 42, "show": True, "rows": []}, {"lang": "ru"})
    with ZipFile(BytesIO(output)) as archive:
        sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    assert "{o. lang = e n}" in "".join(sheet.itertext())
    assert "TITLE" in "".join(sheet.itertext())


@pytest.mark.parametrize("python", [False, True], ids=["native", "python"])
@pytest.mark.parametrize(
    "declaration", ["{o.lang=EN}", "{o. lang = e n}", "{o.lang=en}{o.unknownOption=ignored}{o.styleSource=ignored}"]
)
def test_xlsx_common_option_grammar_and_style_source(
    request: pytest.FixtureRequest, python: bool, declaration: str
) -> None:
    if python:
        request.getfixturevalue("python_only")
    stream = BytesIO()
    with ZipFile(BytesIO(_template())) as source, ZipFile(stream, "w") as destination:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = content.replace(b"{d.title}", declaration.encode() + b"{d.title}").replace(
                    b"{d.total}", b"{d.total:formatN(1)}"
                )
            destination.writestr(name, content)
    engine = Engine()
    assert engine.compile(stream.getvalue()).option_values["lang"] == "en"
    output = engine.render(
        stream.getvalue(), {"title": "TITLE", "total": 1234.5, "show": True, "rows": []}, {"lang": "ru"}
    )
    with ZipFile(BytesIO(output)) as archive:
        sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    assert [node.text for node in sheet.iter(f"{{{_SHEET}}}t") if node.text] == ["TITLE", "VISIBLE", "Total 1,234.5"]
