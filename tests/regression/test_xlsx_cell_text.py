"""Apply Excel's text limit to the final value of each worksheet cell."""

from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from lxml import etree

from carbotek import Engine
from carbotek.errors import CorruptedTemplateError, TemplateError
from carbotek.ooxml.xml import parse_xml
from carbotek.package import TemplatePart
from carbotek.xlsx import cell_text

_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml."
_MAIN = "xl/worksheets/sheet1.xml"
_HIDDEN = "private/values.xml"
_LIMIT = 32_767


def _worksheet(*rows: str) -> str:
    contents = "".join(f'<row r="{index}">{cells}</row>' for index, cells in enumerate(rows, 1))
    return f'<worksheet xmlns="{_SHEET}"><sheetData>{contents}</sheetData></worksheet>'


def _workbook(*rows: str, hidden: str | None = None, shared: str | None = None) -> bytes:
    parts = {_MAIN: _worksheet(*rows)}
    types = {"xl/workbook.xml": "sheet.main", _MAIN: "worksheet"}
    sheets = '<sheet name="Report" sheetId="1" r:id="rId1"/>'
    relationships = f'<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="{_REL}/worksheet"/>'
    if hidden is not None:
        parts[_HIDDEN] = _worksheet(hidden)
        types[_HIDDEN] = "worksheet"
        sheets += '<sheet name="Private" sheetId="2" state="hidden" r:id="rId2"/>'
        relationships += f'<Relationship Id="rId2" Target="../{_HIDDEN}" Type="{_REL}/worksheet"/>'
    if shared is not None:
        parts["xl/sharedStrings.xml"] = f'<sst xmlns="{_SHEET}"><si><t>{escape(shared)}</t></si></sst>'
        types["xl/sharedStrings.xml"] = "sharedStrings"
        relationships += f'<Relationship Id="rId3" Target="sharedStrings.xml" Type="{_REL}/sharedStrings"/>'
    overrides = "".join(
        f'<Override PartName="/{part}" ContentType="{_MIME}{mime}+xml"/>' for part, mime in types.items()
    )
    parts["[Content_Types].xml"] = (
        f'<Types xmlns="{_CONTENT_TYPES}">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f"{overrides}</Types>"
    )
    parts["_rels/.rels"] = (
        f'<Relationships xmlns="{_PACKAGE_REL}">'
        f'<Relationship Id="rId1" Target="xl/workbook.xml" Type="{_REL}/officeDocument"/>'
        "</Relationships>"
    )
    parts["xl/workbook.xml"] = f'<workbook xmlns="{_SHEET}" xmlns:r="{_REL}"><sheets>{sheets}</sheets></workbook>'
    parts["xl/_rels/workbook.xml.rels"] = f'<Relationships xmlns="{_PACKAGE_REL}">{relationships}</Relationships>'
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _read_cell_text(book: bytes, tag: str = "t", part: str = _MAIN) -> list[str]:
    with ZipFile(BytesIO(book)) as archive:
        root = etree.fromstring(archive.read(part))
    return [node.text for node in root.iter(f"{{{_SHEET}}}{tag}")]


@pytest.fixture(params=[False, True], ids=["native", "python"])
def engine(request: pytest.FixtureRequest) -> Engine:
    if request.param:
        request.getfixturevalue("python_only")
    return Engine()


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_rendered_text_limit(engine: Engine, length: int) -> None:
    template = _workbook('<c r="A1" t="inlineStr"><is><t>{d.note}</t></is></c>')
    literal = "{d.other}"
    value = literal + "Я" * (length - len(literal))
    data = {"note": value, "other": "unexpected substitution"}
    if length == _LIMIT:
        assert _read_cell_text(engine.render(template, data)) == [value]
    else:
        with pytest.raises(TemplateError, match=r"32767 UTF-16 code unit limit in 'Report'!A1") as error:
            engine.render(template, data)
        assert literal not in str(error.value)
        assert "Я" not in str(error.value)


def test_diagnostic_escapes_apostrophes_in_sheet_name(engine: Engine) -> None:
    book = _workbook('<c r="A1" t="inlineStr"><is><t>{d.note}</t></is></c>')
    with ZipFile(BytesIO(book)) as archive:
        workbook = archive.read("xl/workbook.xml").replace(b"Report", b"O'Brien")
    book = _rewrite_book(book, {"xl/workbook.xml": workbook})
    with pytest.raises(TemplateError, match=r"'O''Brien'!A1$"):
        engine.render(book, {"note": "A" * (_LIMIT + 1)})


def test_exact_validation_preserves_serialized_worksheet() -> None:
    part = TemplatePart.from_tree(_MAIN, parse_xml(_worksheet().encode(), _MAIN))
    content = part.content

    root = cell_text._worksheet_tree(part, content)
    assert part.content is content

    part.tree().set("changed", "1")
    assert root.get("changed") == "1"
    assert b'changed="1"' in part.content


def test_concatenated_markers_can_exceed_the_limit(engine: Engine) -> None:
    template = _workbook('<c r="A1" t="inlineStr"><is><t>{d.first}{d.second}</t></is></c>')
    with pytest.raises(TemplateError, match="32767"):
        engine.render(template, {"first": "A" * 20_000, "second": "B" * 20_000})


def test_cell_limit_applies_to_rendered_loop_copies(engine: Engine) -> None:
    template = _workbook(
        '<c r="A1" t="inlineStr"><is><t>{d.rows[i].note}</t></is></c>',
        '<c r="A2" t="inlineStr"><is><t>{d.rows[i+1].note}</t></is></c>',
    )
    allowed = "Я" * _LIMIT
    output = engine.render(template, {"rows": [{"note": "short"}, {"note": allowed}]})
    assert _read_cell_text(output) == ["short", allowed]
    with pytest.raises(TemplateError, match="32767"):
        engine.render(template, {"rows": [{"note": "short"}, {"note": allowed + "Я"}]})


def test_unused_input_above_the_limit_can_render(engine: Engine) -> None:
    template = _workbook('<c r="A1" t="inlineStr"><is><t>Report</t></is></c>')
    assert _read_cell_text(engine.render(template, {"unused": "Я" * (_LIMIT + 1)})) == ["Report"]


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_shared_string_limit(engine: Engine, length: int) -> None:
    value = "Я" * length
    template = _workbook('<c r="A1" t="s"><v>0</v></c>', shared=value)
    if length == _LIMIT:
        assert _read_cell_text(engine.render(template, {})) == [value]
    else:
        with pytest.raises(TemplateError, match="32767"):
            engine.render(template, {})


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_hidden_worksheet_rich_text_ignores_phonetic_runs(engine: Engine, length: int) -> None:
    first = "Я" * 20_000
    second = "Ю" * (length - len(first))
    template = _workbook(
        '<c r="A1" t="inlineStr"><is><t>Report</t></is></c>',
        hidden=(
            '<c r="B1" t="inlineStr"><is><r><rPr><b/></rPr><t>{d.first}</t></r>'
            '<r><t>{d.second}</t></r><rPh sb="0" eb="1"><t>ヨ</t></rPh></is></c>'
        ),
    )
    data = {"first": first, "second": second}
    if length == _LIMIT:
        assert _read_cell_text(engine.render(template, data), part=_HIDDEN) == [first, second, "ヨ"]
    else:
        with pytest.raises(TemplateError, match=r"32767 UTF-16 code unit limit in 'Private'!B1"):
            engine.render(template, data)


def test_string_formula_cache_can_exceed_the_text_limit(engine: Engine) -> None:
    template = _workbook(
        '<c r="A1" t="str"><f>REPT("Я",32767)</f><v>{d.cached}</v></c><c r="B1" t="n"><f>1+1</f><v>2</v></c>'
    )
    value = "Я" * (_LIMIT + 1)
    assert _read_cell_text(engine.render(template, {"cached": value}), "v") == [value, "2"]


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_exact_reparse_ignores_large_formula_cache(engine: Engine, length: int) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c><c t="str"><f>"Report"</f><v>{d.cached}</v></c>')
    literal = "{d.literal}"
    note = literal + "文" * (length - len(literal))
    cached = "X" * 11_000_000
    if length > _LIMIT:
        with pytest.raises(TemplateError, match=r"'Report'!A1$"):
            engine.render(template, {"note": note, "cached": cached})
        return
    with ZipFile(BytesIO(engine.render(template, {"note": note, "cached": cached}))) as archive:
        content = archive.read(_MAIN)
    assert b"<t>" + note.encode() + b"</t>" in content
    assert b"<v>" + cached.encode() + b"</v>" in content


def test_exact_reparse_still_rejects_dtd() -> None:
    content = (
        b"<!DOCTYPE worksheet>" + _worksheet('<c t="str"><f>"Report"</f><v>' + "X" * 11_000_000 + "</v></c>").encode()
    )
    with pytest.raises(CorruptedTemplateError):
        cell_text._worksheet_tree(TemplatePart(_MAIN, content, True), content)


@pytest.mark.parametrize("text", ["Report", "文" * 16_384], ids=["short", "multibyte"])
def test_inline_validation_allows_a_following_large_formula_cache(engine: Engine, text: str) -> None:
    template = _workbook(
        f'<c t="inlineStr"><is><t>{text}</t></is></c><c t="str"><f>"Report"</f><v>{{d.cached}}</v></c>'
    )
    value = "X" * 11_000_000
    output = engine.render(template, {"cached": value})
    with ZipFile(BytesIO(output)) as archive:
        assert b"<v>" + value.encode() + b"</v>" in archive.read(_MAIN)


def test_very_long_rendered_inline_text_keeps_the_excel_diagnostic(engine: Engine) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    with pytest.raises(TemplateError, match=r"32767 UTF-16 code unit limit in 'Report'!A1"):
        engine.render(template, {"note": "X" * 11_000_000})


@pytest.mark.parametrize("supplementary", ["😀", "_xD83D__xDE00_"], ids=["literal", "escaped"])
def test_supplementary_characters_use_two_utf16_units(engine: Engine, supplementary: str) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    allowed = supplementary * 16_383 + "a"
    assert _read_cell_text(engine.render(template, {"note": allowed})) == [allowed]
    for too_long in (supplementary * 16_384, supplementary + "a" * 32_766):
        with pytest.raises(TemplateError, match="32767 UTF-16"):
            engine.render(template, {"note": too_long})


def _rewrite_book(book: bytes, replacements: dict[str, bytes], rename: dict[str, str] | None = None) -> bytes:
    stream = BytesIO()
    with ZipFile(BytesIO(book)) as source, ZipFile(stream, "w") as target:
        parts = {name: source.read(name) for name in source.namelist()}
        parts.update(replacements)
        for name, content in parts.items():
            target.writestr((rename or {}).get(name, name), content)
    return stream.getvalue()


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_validates_shared_strings_kept_in_a_linked_part(engine: Engine, length: int) -> None:
    template = _workbook("<c r=\"A1\" t='s'><v>0</v></c>", shared="{d.note}")
    with ZipFile(BytesIO(template)) as archive:
        parts = {
            name: archive.read(name).replace(b"sharedStrings.xml", b"cell-values.xml") for name in archive.namelist()
        }
    template = _rewrite_book(template, parts, {"xl/sharedStrings.xml": "xl/cell-values.xml"})
    value = "😀" + "Я" * (length - 2)
    if length > _LIMIT:
        with pytest.raises(TemplateError, match=r"'Report'!A1$"):
            engine.render(template, {"note": value})
    else:
        output = engine.render(template, {"note": value})
        assert _read_cell_text(output, part="xl/cell-values.xml") == [value]


def test_unused_oversized_shared_string_does_not_reject_workbook(engine: Engine) -> None:
    template = _workbook("<c r=\"A1\" t='s'><v>1</v></c>", shared="{d.note}")
    with ZipFile(BytesIO(template)) as archive:
        parts = {
            name: archive.read(name).replace(b"sharedStrings.xml", b"cell-values.xml") for name in archive.namelist()
        }
    parts["xl/sharedStrings.xml"] = parts["xl/sharedStrings.xml"].replace(b"</sst>", b"<si><t>short</t></si></sst>")
    template = _rewrite_book(template, parts, {"xl/sharedStrings.xml": "xl/cell-values.xml"})
    output = engine.render(template, {"note": "X" * (_LIMIT + 1)})
    assert _read_cell_text(output, "v") == ["1"]
    assert _read_cell_text(output, part="xl/cell-values.xml")[1] == "short"


def test_bom_does_not_force_an_exact_worksheet_parse(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>'
    content += _worksheet('<c t="inlineStr"><is><t>short</t></is></c>').encode()
    template = _rewrite_book(_workbook(""), {_MAIN: content})

    def unexpected_parse(part: TemplatePart, content: bytes) -> etree._Element:
        pytest.fail(f"Unexpected exact validation for {part.name}")

    monkeypatch.setattr(cell_text, "_worksheet_tree", unexpected_parse)
    assert _read_cell_text(engine.render(template, {})) == ["short"]


@pytest.mark.parametrize("kind", ["plain", "rich"])
@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_excel_escaped_text_limit(engine: Engine, kind: str, length: int) -> None:
    if kind == "rich":
        cell = '<c t="inlineStr"><is><r><t>Я</t></r><r><t>{d.note}</t></r></is></c>'
    else:
        cell = '<c t="inlineStr"><is><t>Я{d.note}</t></is></c>'
    value = "Я" * (length - 3) + "_x000D_Я"
    template = _workbook(cell)
    if length == _LIMIT:
        output = engine.render(template, {"note": value})
        assert "".join(_read_cell_text(output)) == "Я" + value
    else:
        with pytest.raises(TemplateError, match="32767"):
            engine.render(template, {"note": value})


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_excel_escaped_underscore_is_not_decoded_twice(engine: Engine, length: int) -> None:
    # _x005F_ escapes the underscore of the literal seven-character string _x000D_.
    value = "Я" * (length - 7) + "_x005F_x000D_"
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    if length == _LIMIT:
        assert _read_cell_text(engine.render(template, {"note": value})) == [value]
    else:
        with pytest.raises(TemplateError, match="32767"):
            engine.render(template, {"note": value})


def test_unrelated_xml_is_not_validated_as_a_worksheet(engine: Engine) -> None:
    template = _rewrite_book(
        _workbook('<c t="inlineStr"><is><t>Report</t></is></c>'),
        {
            "customXml/item1.xml": b"<text>{d.note}</text>",
            "customXml/item2.xml": _worksheet('<c t="inlineStr"><is><t>{d.other}</t></is></c>').encode(),
        },
    )
    value = "A" * 11_000_000
    output = engine.render(template, {"note": value, "other": "B" * (_LIMIT + 1)})
    with ZipFile(BytesIO(output)) as archive:
        assert value.encode() in archive.read("customXml/item1.xml")
        assert ("B" * (_LIMIT + 1)).encode() in archive.read("customXml/item2.xml")
    assert _read_cell_text(output) == ["Report"]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_linked_worksheet_at_non_xml_path(engine: Engine, strict: bool, length: int) -> None:
    # Rendering non-.xml parts is not supported; validate their already stored cell values.
    value = "Я" * length
    template = _workbook(f'<c t="inlineStr"><is><t>{value}</t></is></c>')
    with ZipFile(BytesIO(template)) as archive:
        parts = {name: archive.read(name).replace(b"sheet1.xml", b"sheet1.data") for name in archive.namelist()}
    if strict:
        parts = {
            name: content.replace(_SHEET.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main").replace(
                _REL.encode(), b"http://purl.oclc.org/ooxml/officeDocument/relationships"
            )
            for name, content in parts.items()
        }
    template = _rewrite_book(template, parts, {_MAIN: "xl/worksheets/sheet1.data"})
    if length == _LIMIT:
        with ZipFile(BytesIO(engine.render(template, {}))) as archive:
            assert value.encode() in archive.read("xl/worksheets/sheet1.data")
    else:
        with pytest.raises(TemplateError, match=r"32767.*'Report'!A1"):
            engine.render(template, {})


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_strict_inline_worksheet_after_substitution(engine: Engine, length: int) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    with ZipFile(BytesIO(template)) as archive:
        parts = {
            name: archive.read(name)
            .replace(_SHEET.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main")
            .replace(_REL.encode(), b"http://purl.oclc.org/ooxml/officeDocument/relationships")
            for name in archive.namelist()
        }
    template = _rewrite_book(template, parts)
    value = "Я" * length
    if length == _LIMIT:
        with ZipFile(BytesIO(engine.render(template, {"note": value}))) as archive:
            assert value.encode() in archive.read(_MAIN)
    else:
        with pytest.raises(TemplateError, match="32767"):
            engine.render(template, {"note": value})


@pytest.mark.parametrize("length", [_LIMIT, _LIMIT + 1])
def test_relationship_targets_use_opc_case_equivalence(engine: Engine, length: int) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    with ZipFile(BytesIO(template)) as archive:
        parts = {
            "_rels/.rels": archive.read("_rels/.rels").replace(b"xl/workbook.xml", b"/XL/WORKBOOK.XML"),
            "xl/_rels/workbook.xml.rels": archive.read("xl/_rels/workbook.xml.rels").replace(
                b"worksheets/sheet1.xml", b"/XL/WORKSHEETS/SHEET1.XML"
            ),
        }
    template = _rewrite_book(template, parts, {"xl/_rels/workbook.xml.rels": "xl/_rels/WORKBOOK.XML.rels"})
    value = "Я" * length
    if length == _LIMIT:
        assert _read_cell_text(engine.render(template, {"note": value})) == [value]
    else:
        with pytest.raises(TemplateError, match="32767"):
            engine.render(template, {"note": value})


@pytest.mark.parametrize("legacy", [False, True])
def test_inferred_reference_depends_on_preserving_gaps(engine: Engine, legacy: bool) -> None:
    option = "{o.preReleaseFeatureIn=0}" if legacy else ""
    template = _workbook(f'<c r="AA3" t="inlineStr"><is><t>{option}{{d.note}}</t></is></c>')
    with ZipFile(BytesIO(template)) as archive:
        sheet = archive.read(_MAIN).replace(b'<row r="1">', b'<row r="3">')
    template = _rewrite_book(template, {_MAIN: sheet})
    location = "'Report'" if legacy else "'Report'!AA3"
    with pytest.raises(TemplateError, match=rf"{location}$"):
        engine.render(template, {"note": "A" * (_LIMIT + 1)})


@pytest.mark.parametrize("prefixed", [False, True])
def test_short_inline_text_skips_worksheet_reparse(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, prefixed: bool
) -> None:
    template = _workbook('<c t="inlineStr"><is><t>{d.note}</t></is></c>')
    if prefixed:
        sheet = (
            f'<s:worksheet xmlns:s="{_SHEET}"><s:sheetData><s:row>'
            '<s:c t="inlineStr"><s:is><s:t>{d.note}</s:t></s:is></s:c>'
            "</s:row></s:sheetData></s:worksheet>"
        )
        template = _rewrite_book(template, {_MAIN: sheet.encode()})
    parse_xml = cell_text.parse_xml

    def parse_package_xml(content: bytes, name: str) -> etree._Element:
        assert name != _MAIN
        return parse_xml(content, name)

    monkeypatch.setattr(cell_text, "parse_xml", parse_package_xml)
    assert _read_cell_text(engine.render(template, {"note": "short"})) == ["short"]


def test_prefilter_keeps_possible_overflow_in_mixed_namespace_runs(engine: Engine) -> None:
    value = "a" * 16_384
    template = _workbook(
        f'<c t="inlineStr"><p:is xmlns:p="{_SHEET}" ><p:r><p:t>{value}</p:t></p:r>'
        f"<r><t>{value}</t></r></p:is></c>"
        '<c t="inlineStr"><is><t>short</t></is></c>'
    )
    with pytest.raises(TemplateError, match=r"'Report'!A1"):
        engine.render(template, {})


def test_markup_inside_cdata_cannot_hide_an_oversized_cell(engine: Engine) -> None:
    half = "a" * 16_384
    template = _workbook(f'<c t="inlineStr"><is><t><![CDATA[{half}<is>{half}]]></t></is></c>')
    with pytest.raises(TemplateError, match=r"'Report'!A1"):
        engine.render(template, {})
