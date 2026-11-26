"""HTML render uses the style and numbering parts linked from the main DOCX document."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from lxml import etree
from test_render_session import document, paragraph, root

from carbotek import Engine
from carbotek.errors import CorruptedTemplateError
from carbotek.ooxml.ns import NAMESPACES, qn

_REL_TYPES = NAMESPACES["r"]
_RELS_NAME = "word/_rels/document.xml.rels"
_MANIFEST = "[Content_Types].xml"
_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_KINDS = ("styles", "numbering")
_STYLES = (
    f'<w:styles xmlns:w="{NAMESPACES["w"]}">'
    '<w:style w:type="paragraph" w:styleId="CustomHeading"><w:name w:val="heading 1"/></w:style>'
    "</w:styles>"
)
_NUMBERING = (
    f'<w:numbering xmlns:w="{NAMESPACES["w"]}">'
    '<w:abstractNum w:abstractNumId="1000"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
    '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1000"><w:abstractNumId w:val="1000"/></w:num>'
    "</w:numbering>"
)
_EXISTING_BODY = (
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1000"/></w:numPr></w:pPr>'
    "<w:r><w:t>Old numbered item</w:t></w:r></w:p>"
    '<w:p><w:hyperlink r:id="rIdExisting"><w:r><w:t>Old link</w:t></w:r></w:hyperlink></w:p>'
)
_HTML = '<h1>Section</h1><ul><li>Bullet</li></ul><p><a href="https://example.org/new">New link</a></p>'


def _relationship(kind: str, target: str, relationship_id: str, mode: str = "Internal") -> str:
    return f'<p:Relationship Id="{relationship_id}" Type="{_REL_TYPES}/{kind}" Target="{target}" TargetMode="{mode}"/>'


def _relationships(*nodes: str) -> str:
    return f'<p:Relationships xmlns:p="{NAMESPACES["rel"]}">{"".join(nodes)}</p:Relationships>'


def _manifest(**parts: str) -> str:
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{_MIME}{kind}+xml"/>' for kind, name in parts.items()
    )
    return (
        f'<Types xmlns="{NAMESPACES["ct"]}">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{_MIME}document.main+xml"/>'
        f"{overrides}</Types>"
    )


def _values(xml: etree._Element, tag: str) -> list[str]:
    return [node.get(qn("w:val"), "") for node in xml.iter(qn(f"w:{tag}"))]


def _number_formats(xml: etree._Element) -> dict[str, tuple[str, str]]:
    formats = {
        node.get(qn("w:abstractNumId"), ""): _values(node, "numFmt")[0] for node in xml.findall(qn("w:abstractNum"))
    }
    result = {}
    for number in xml.findall(qn("w:num")):
        abstract_id = _values(number, "abstractNumId")[0]
        result[number.get(qn("w:numId"), "")] = abstract_id, formats[abstract_id]
    return result


@pytest.mark.parametrize(
    ("targets", "paths"),
    [
        (("styles.xml", "numbering.xml"), ("word/styles.xml", "word/numbering.xml")),
        (
            ("../parts/active-styles.xml", "../parts/active-numbering.xml"),
            ("parts/active-styles.xml", "parts/active-numbering.xml"),
        ),
        (
            ("/PARTS/ACTIVE-STYLES.xml", "/PARTS/ACTIVE-NUMBERING.xml"),
            ("Parts/Active-Styles.xml", "Parts/Active-Numbering.xml"),
        ),
        ((), ("word/styles.xml", "word/numbering.xml")),
    ],
    ids=["standard", "relative", "absolute-ascii-case", "absent-links"],
)
def test_html_uses_linked_styles_and_numbering(targets: tuple[str, ...], paths: tuple[str, str]) -> None:
    styles, numbering = paths
    rels_name = "word/_RELS/document.xml.rels" if targets and targets[0].startswith("/") else _RELS_NAME
    relations = [_relationship("hyperlink", "https://example.org/old", "rIdExisting", "External")]
    if targets:
        relations.extend(
            _relationship(kind, target, f"rId{kind.title()}") for kind, target in zip(_KINDS, targets, strict=True)
        )
    # Use differently cased existing overrides and keep one declaration per active part.
    declarations = "".join(
        f'<m:Override PartName="/{path.upper()}" ContentType="{_MIME}{kind}+xml"/>'
        for kind, path in zip(_KINDS, paths, strict=True)
    )
    manifest = (
        f'<m:Types xmlns:m="{NAMESPACES["ct"]}">'
        '<m:Default Extension="xml" ContentType="application/xml"/>'
        '<m:Default Extension="RELS" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<m:Override PartName="/word/document.xml" ContentType="{_MIME}document.main+xml"/>'
        f"{declarations}</m:Types>"
    )
    template = document(
        _EXISTING_BODY + paragraph("{d.html:html}"),
        **{styles: _STYLES, numbering: _NUMBERING, rels_name: _relationships(*relations), _MANIFEST: manifest},
    )
    output = Engine().render(template, {"html": _HTML})
    rendered = root(output)
    assert _values(rendered, "pStyle") == ["CustomHeading"]
    assert [style.get(qn("w:styleId")) for style in root(output, styles).findall(qn("w:style"))] == [
        "CustomHeading",
        "Hyperlink",
    ]
    existing_id, bullet_id = _values(rendered, "numId")
    assert existing_id == "1000"
    assert bullet_id != existing_id
    numbering_formats = _number_formats(root(output, numbering))
    assert numbering_formats[existing_id] == ("1000", "decimal")
    assert numbering_formats[bullet_id][0] != "1000"
    assert numbering_formats[bullet_id][1] == "bullet"

    relations_by_type = {}
    for node in root(output, rels_name).findall(qn("rel:Relationship")):
        relations_by_type.setdefault(node.get("Type"), []).append(node)
    for kind, target in zip(_KINDS, targets or ("styles.xml", "numbering.xml"), strict=True):
        (relationship,) = relations_by_type[f"{_REL_TYPES}/{kind}"]
        assert relationship.get("Target") == target
        assert relationship.get("TargetMode", "Internal") == "Internal"
        if targets:
            assert relationship.get("Id") == f"rId{kind.title()}"
    hyperlinks = {node.get("Id"): node.get("Target") for node in relations_by_type[f"{_REL_TYPES}/hyperlink"]}
    old_link, new_link = list(rendered.iter(qn("w:hyperlink")))
    assert old_link.get(qn("r:id")) == "rIdExisting"
    assert hyperlinks == {"rIdExisting": "https://example.org/old", new_link.get(qn("r:id")): "https://example.org/new"}
    manifest_root = root(output, _MANIFEST)
    assert len(manifest_root.findall(qn("ct:Default"))) == 2
    assert [node.get("PartName") for node in manifest_root.findall(qn("ct:Override"))] == [
        "/word/document.xml",
        f"/{styles.upper()}",
        f"/{numbering.upper()}",
    ]
    with ZipFile(BytesIO(output)) as archive:
        if styles != "word/styles.xml":
            assert "word/styles.xml" not in archive.namelist()
            assert "word/numbering.xml" not in archive.namelist()
        if rels_name != _RELS_NAME:
            assert _RELS_NAME not in archive.namelist()


def test_html_registers_missing_style_and_numbering_parts() -> None:
    output = Engine().render(document(paragraph("{d.html:html}")), {"html": _HTML})
    assert _values(root(output), "pStyle") == ["Heading1"]
    (bullet_id,) = _values(root(output), "numId")
    assert _number_formats(root(output, "word/numbering.xml"))[bullet_id][1] == "bullet"
    rels = root(output, _RELS_NAME)
    assert len({node.get("Id") for node in rels}) == len(rels) == 3
    for kind in _KINDS:
        (related,) = [node for node in rels if node.get("Type") == f"{_REL_TYPES}/{kind}"]
        assert related.get("Target") == f"{kind}.xml"
        name = f"/word/{kind}.xml"
        (override,) = [
            node for node in root(output, _MANIFEST).findall(qn("ct:Override")) if node.get("PartName") == name
        ]
        assert override.get("ContentType") == f"{_MIME}{kind}+xml"


def test_html_registers_unlinked_styles_when_existing_heading_is_reused() -> None:
    template = document(paragraph("{d.html:html}"), **{"word/styles.xml": _STYLES})
    output = Engine().render(template, {"html": "<h1>Section</h1>"})
    assert _values(root(output), "pStyle") == ["CustomHeading"]
    with ZipFile(BytesIO(output)) as archive:
        assert archive.read("word/styles.xml") == _STYLES.encode()
    (relationship,) = root(output, _RELS_NAME).findall(qn("rel:Relationship"))
    assert relationship.get("Type") == f"{_REL_TYPES}/styles"
    assert relationship.get("Target") == "styles.xml"


@pytest.mark.parametrize(
    ("target", "part_name"),
    [
        ("/parts/%D1%81%D1%82%D0%B8%D0%BB%D0%B8.xml", "parts/стили.xml"),
        ("/parts/%D1%81%D1%82%D0%B8%D0%BB%D0%B8.xml", "parts/%D1%81%D1%82%D0%B8%D0%BB%D0%B8.xml"),
        ("/parts/active:styles.xml", "parts/active:styles.xml"),
    ],
    ids=["unicode-zip-name", "percent-encoded-zip-name", "colon-in-uri-path"],
)
def test_html_recognizes_linked_part_uri_and_manifest_override(target: str, part_name: str) -> None:
    rels = _relationships(_relationship("styles", target, "rIdStyles"))
    manifest = (
        f'<Types xmlns="{NAMESPACES["ct"]}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{_MIME}document.main+xml"/>'
        f'<Override PartName="{target}" ContentType="{_MIME}styles+xml"/>'
        "</Types>"
    )
    template = document(paragraph("{d.html:html}"), **{_RELS_NAME: rels, part_name: _STYLES, _MANIFEST: manifest})
    output = Engine().render(template, {"html": "<h1>Section</h1><h2>Subsection</h2>"})
    assert _values(root(output), "pStyle") == ["CustomHeading", "Heading2"]
    assert [node.get(qn("w:styleId")) for node in root(output, part_name).findall(qn("w:style"))] == [
        "CustomHeading",
        "Heading2",
    ]
    with ZipFile(BytesIO(output)) as archive:
        assert archive.read(_RELS_NAME) == rels.encode()
        assert archive.read(_MANIFEST) == manifest.encode()
        assert {name for name in archive.namelist() if name.startswith("parts/")} == {part_name}


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("target", ["../parts/missing.xml", "/parts/%D1%81.xml"])
def test_html_rejects_missing_linked_part_only_when_used(kind: str, target: str) -> None:
    rels = _relationships(_relationship(kind, target, "rIdPart"))
    template = document(paragraph("{d.html:html}"), **{_RELS_NAME: rels})
    Engine().render(template, {"html": "<p>Plain</p>"})
    with pytest.raises(CorruptedTemplateError, match=f"Missing DOCX {kind} relationship target"):
        Engine().render(template, {"html": _HTML})


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("linked", [True, False])
def test_html_rejects_incompatible_part_override(kind: str, linked: bool) -> None:
    part_name = f"word/{kind}.xml"
    manifest = _manifest(**{kind: part_name}).replace(f"{_MIME}{kind}+xml", "application/xml")
    rels = _relationships(*([_relationship(kind, f"{kind}.xml", "rIdPart")] if linked else []))
    template = document(
        paragraph("{d.html:html}"),
        **{
            _RELS_NAME: rels,
            _MANIFEST: manifest,
            part_name: _STYLES if kind == "styles" else _NUMBERING,
        },
    )
    Engine().render(template, {"html": "<p>Plain</p>"})
    with pytest.raises(CorruptedTemplateError, match=f"Invalid DOCX {kind} ContentType"):
        Engine().render(template, {"html": _HTML})


def test_html_preserves_existing_numbering_ids_and_cleanup_order() -> None:
    part_name = "parts/numbering.xml"
    rels = _relationships(
        _relationship("numbering", f"/{part_name}", "rIdNumbering"),
        _relationship("hyperlink", "https://example.org/old", "rIdExisting", "External"),
    )
    numbering = _NUMBERING.replace('w:abstractNumId="1000"', 'w:abstractNumId=" +1000 "').replace(
        '<w:num w:numId="1000"><w:abstractNumId w:val="1000"/></w:num>',
        '<w:num w:numId="+1000"><w:abstractNumId w:val="1000"/></w:num><w:numIdMacAtCleanup w:val="1000"/>',
    )
    template = document(
        _EXISTING_BODY + paragraph("{d.html:html}"),
        **{_RELS_NAME: rels, part_name: numbering, _MANIFEST: _manifest(numbering=part_name)},
    )
    output = Engine().render(template, {"html": "<ul><li>Bullet</li></ul>"})
    rendered = root(output, part_name)
    assert [child.tag for child in rendered] == [
        qn(f"w:{tag}") for tag in ("abstractNum", "abstractNum", "num", "num", "numIdMacAtCleanup")
    ]
    assert [node.get(qn("w:abstractNumId")) for node in rendered.findall(qn("w:abstractNum"))] == [" +1000 ", "1001"]
    assert [node.get(qn("w:numId")) for node in rendered.findall(qn("w:num"))] == ["+1000", "1001"]
    assert _values(root(output), "numId") == ["1000", "1001"]
    assert _values(rendered, "numFmt") == ["decimal", "bullet"]


def test_html_uses_free_numbering_id_when_maximum_exists() -> None:
    part_name = "parts/numbering.xml"
    rels = _relationships(_relationship("numbering", f"/{part_name}", "rIdNumbering"))
    template = document(
        paragraph("{d.html:html}"),
        **{
            _RELS_NAME: rels,
            part_name: _NUMBERING.replace("1000", "2147483647"),
            _MANIFEST: _manifest(numbering=part_name),
        },
    )
    output = Engine().render(template, {"html": "<ul><li>Bullet</li></ul>"})
    (bullet_id,) = _values(root(output), "numId")
    assert bullet_id == "1000"
    assert _number_formats(root(output, part_name)) == {
        "2147483647": ("2147483647", "decimal"),
        bullet_id: (bullet_id, "bullet"),
    }


@pytest.mark.parametrize(
    "target", [".", " ", "/parts%2fstyles.xml", "/parts/%2e%2e/styles.xml", "https://example.org/styles.xml"]
)
def test_html_rejects_non_part_relationship_targets(target: str) -> None:
    rels = _relationships(_relationship("styles", target, "rIdStyles"))
    template = document(paragraph("{d.html:html}"), **{_RELS_NAME: rels})
    with pytest.raises(CorruptedTemplateError, match="Invalid DOCX styles relationship target"):
        Engine().render(template, {"html": "<h1>Section</h1>"})


@pytest.mark.parametrize("kind", _KINDS)
def test_html_rejects_external_part_relationship_when_used(kind: str) -> None:
    rels = _relationships(_relationship(kind, f"{kind}.xml", "rIdExternal", "External"))
    template = document(paragraph("{d.html:html}"), **{_RELS_NAME: rels})
    assert root(Engine().render(template, {"html": "<p>Plain</p>"}), _RELS_NAME)[0].get("TargetMode") == "External"
    with pytest.raises(CorruptedTemplateError, match=f"DOCX {kind} relationship must be internal"):
        Engine().render(template, {"html": _HTML})


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("mime", ["valid", "uppercase", "wrong", "missing"])
@pytest.mark.parametrize("stem", ["regular", ""])
def test_html_resolves_linked_part_default_content_type(kind: str, mime: str, stem: str) -> None:
    part_name = f"parts/{stem}.part"
    manifest = _manifest()
    if mime != "missing":
        content_type = f"{_MIME}{kind}+xml" if mime in {"valid", "uppercase"} else "application/xml"
        if mime == "uppercase":
            content_type = content_type.upper()
        manifest = manifest.replace("</Types>", f'<Default Extension="PART" ContentType="{content_type}"/></Types>')
    rels = _relationships(_relationship(kind, f"/{part_name}", "rIdPart"))
    template = document(
        paragraph("{d.html:html}"),
        **{_RELS_NAME: rels, _MANIFEST: manifest, part_name: _STYLES if kind == "styles" else _NUMBERING},
    )
    Engine().render(template, {"html": "<p>Plain</p>"})
    html = "<h1>Section</h1>" if kind == "styles" else "<ul><li>Bullet</li></ul>"
    if mime not in {"valid", "uppercase"}:
        with pytest.raises(CorruptedTemplateError, match=f"Invalid DOCX {kind} ContentType"):
            Engine().render(template, {"html": html})
    else:
        output = Engine().render(template, {"html": html})
        with ZipFile(BytesIO(output)) as archive:
            assert archive.read(_MANIFEST) == manifest.encode()


@pytest.mark.parametrize("linked", [True, False])
def test_html_accepts_case_equivalent_content_type_override(linked: bool) -> None:
    manifest = _manifest(styles="word/styles.xml").replace(f"{_MIME}styles+xml", f"{_MIME}styles+xml".upper())
    rels = _relationships(*([_relationship("styles", "styles.xml", "rIdStyles")] if linked else []))
    template = document(
        paragraph("{d.html:html}"),
        **{
            _MANIFEST: manifest,
            _RELS_NAME: rels,
            "word/styles.xml": _STYLES,
        },
    )
    output = Engine().render(template, {"html": "<h1>Section</h1>"})
    assert _values(root(output), "pStyle") == ["CustomHeading"]
    with ZipFile(BytesIO(output)) as archive:
        assert archive.read(_MANIFEST) == manifest.encode()


def test_html_does_not_use_default_for_extensionless_part() -> None:
    manifest = _manifest().replace("</Types>", f'<Default Extension="styles" ContentType="{_MIME}styles+xml"/></Types>')
    template = document(
        paragraph("{d.html:html}"),
        **{
            _MANIFEST: manifest,
            "styles": _STYLES,
            _RELS_NAME: _relationships(_relationship("styles", "/styles", "rIdStyles")),
        },
    )
    with pytest.raises(CorruptedTemplateError, match="Invalid DOCX styles ContentType"):
        Engine().render(template, {"html": "<h1>Section</h1>"})


@pytest.mark.parametrize("rels_name", [_RELS_NAME, "word/_RELS/document.xml.rels"])
def test_html_image_preserves_all_document_relationships(rels_name: str) -> None:
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/v8sAAAAASUVORK5CYII="
    template = document(
        paragraph("{d.html:html}"),
        **{
            rels_name: _relationships(
                _relationship("styles", "styles.xml", "rIdStyles"),
                _relationship("numbering", "numbering.xml", "rIdNumbering"),
            ),
            "word/styles.xml": _STYLES,
            "word/numbering.xml": _NUMBERING,
            _MANIFEST: _manifest(styles="word/styles.xml", numbering="word/numbering.xml"),
        },
    )
    output = Engine().render(template, {"html": _HTML + f'<p><img src="data:image/png;base64,{png}"></p>'})
    with ZipFile(BytesIO(output)) as archive:
        assert [name for name in archive.namelist() if name.lower() == _RELS_NAME] == [rels_name]
        relationships = {node.get("Id"): node for node in root(output, rels_name)}
        assert {node.get("Type") for node in relationships.values()} == {
            f"{_REL_TYPES}/{kind}" for kind in ("styles", "numbering", "hyperlink", "image")
        }
        rendered = root(output)
        assert _values(rendered, "pStyle") == ["CustomHeading"]
        (bullet_id,) = _values(rendered, "numId")
        assert _number_formats(root(output, "word/numbering.xml"))[bullet_id][1] == "bullet"
        (hyperlink,) = list(rendered.iter(qn("w:hyperlink")))
        assert relationships[hyperlink.get(qn("r:id"))].get("Target") == "https://example.org/new"
        (image,) = list(rendered.iter(qn("a:blip")))
        image_target = relationships[image.get(qn("r:embed"))].get("Target")
        assert f"word/{image_target}" in archive.namelist()
