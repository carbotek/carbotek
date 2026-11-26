"""HTML blocks take their look from the template: generated parts name no font of their own."""

from lxml import etree
from test_html_relationships import _manifest
from test_render_session import document, root

from carbotek import Engine
from carbotek.ooxml.ns import NAMESPACES, qn

FONT = "Body Font"
HTML = (
    '<h1>Section</h1><p>Text with <a href="https://example.com/">a link</a>.</p>'
    "<ul><li>One<ul><li>Two<ul><li>Three<ul><li>Four</li></ul></li></ul></li></ul></li></ul>"
    "<h3>Subsection</h3><ol><li>First</li></ol>"
    "<table><tr><td>Cell</td></tr></table>"
)
_RUN_FONTS = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/>'
_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="styles.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"/>'
    "</Relationships>"
)


def _style(kind: str, style_id: str, name: str) -> str:
    return f'<w:style w:type="{kind}" w:styleId="{style_id}"><w:name w:val="{name}"/></w:style>'


def _template(*styles: str, normal: str = "Normal", with_styles: bool = True) -> bytes:
    marker = f'<w:p><w:r><w:rPr>{_RUN_FONTS}<w:sz w:val="22"/></w:rPr><w:t>{{d.html:html}}</w:t></w:r></w:p>'
    if not with_styles:
        return document(marker)
    styles_xml = (
        f'<w:styles xmlns:w="{NAMESPACES["w"]}">'
        f"<w:docDefaults><w:rPrDefault><w:rPr>{_RUN_FONTS}</w:rPr></w:rPrDefault></w:docDefaults>"
        f'<w:style w:type="paragraph" w:default="1" w:styleId="{normal}"><w:name w:val="Normal"/></w:style>'
        + "".join(styles)
        + "</w:styles>"
    )
    return document(
        marker,
        **{
            "word/styles.xml": styles_xml,
            "word/_rels/document.xml.rels": _RELS,
            "[Content_Types].xml": _manifest(styles="word/styles.xml"),
        },
    )


def _style_ids(output: bytes) -> list[str]:
    return [style.get(qn("w:styleId"), "") for style in root(output, "word/styles.xml").iter(qn("w:style"))]


def _values(output: bytes, tag: str) -> list[str]:
    return [node.get(qn("w:val"), "") for node in root(output).iter(qn(tag))]


def _child_value(node: etree._Element, *path: str, attribute: str = "w:val") -> str | None:
    found = node.find("/".join(qn(step) for step in path))
    return None if found is None else found.get(qn(attribute))


def test_generated_parts_name_only_the_template_font() -> None:
    output = Engine().render(_template(), {"html": HTML})
    fonts = {
        value
        for part in ("word/document.xml", "word/styles.xml", "word/numbering.xml")
        for node in root(output, part).iter(qn("w:rFonts"))
        for key, value in node.attrib.items()
        if etree.QName(key).localname in {"ascii", "hAnsi", "cs", "eastAsia"}
    }
    assert fonts == {FONT}


def test_bullets_are_drawn_in_the_item_font() -> None:
    output = Engine().render(_template(), {"html": HTML})
    levels = [
        level
        for level in root(output, "word/numbering.xml").iter(qn("w:lvl"))
        if _child_value(level, "w:numFmt") == "bullet"
    ]
    assert [_child_value(level, "w:lvlText") for level in levels] == [
        "\N{BULLET}",
        "\N{EN DASH}",
        "\N{MIDDLE DOT}",
        "\N{BULLET}",
    ]
    assert all(level.find(qn("w:rPr")) is None for level in levels)
    items = [p for p in root(output).iter(qn("w:p")) if p.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None]
    assert len(items) == 5
    for item in items:  # the paragraph mark carries the marker's run style, which the label is drawn with
        mark = item.find(f"{qn('w:pPr')}/{qn('w:rPr')}")
        assert mark is not None
        assert [etree.QName(child).localname for child in mark] == ["rFonts", "sz"]
        assert _child_value(mark, "w:rFonts", attribute="w:ascii") == FONT


def test_localized_builtin_styles_are_found_by_name() -> None:
    # Ids as Russian Word writes them; the names stay the English built-in ones.
    template = _template(
        _style("paragraph", "1", "heading 1"),
        _style("paragraph", "3", "heading 3"),
        _style("character", "a5", "Hyperlink"),
        _style("table", "a3", "Table Grid"),
        normal="a",
    )
    output = Engine().render(template, {"html": HTML})
    assert _values(output, "w:pStyle") == ["1", "3"]
    assert set(_values(output, "w:rStyle")) == {"a5"}
    assert _values(output, "w:tblStyle") == ["a3"]
    assert _style_ids(output) == ["a", "1", "3", "a5", "a3"]


def test_fallback_styles_are_added_only_for_what_the_output_uses() -> None:
    output = Engine().render(_template(_style("paragraph", "2", "heading 2"), normal="a"), {"html": HTML})
    assert _style_ids(output) == ["a", "2", "Hyperlink", "Heading1", "Heading3", "TableGrid"]
    for style in root(output, "word/styles.xml").iter(qn("w:style")):
        if style.get(qn("w:styleId")) in {"Heading1", "Heading3"}:
            assert _child_value(style, "w:basedOn") == "a"
            assert style.find(f"{qn('w:rPr')}/{qn('w:rFonts')}") is None


def test_fallback_styles_without_a_styles_part() -> None:
    output = Engine().render(_template(with_styles=False), {"html": "<h2>Title</h2>"})
    assert _style_ids(output) == ["Heading2"]
    heading = root(output, "word/styles.xml").find(qn("w:style"))
    assert heading is not None
    assert _child_value(heading, "w:basedOn") == "Normal"
    assert _values(output, "w:pStyle") == ["Heading2"]
