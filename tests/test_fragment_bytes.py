"""UTF-8 fragments remain searchable and materializable throughout a render."""

import pytest
from lxml import etree

from carbotek import _native_adapter
from carbotek.docx.strings import StringSection, materialize, rendered_placeholder
from carbotek.errors import CorruptedTemplateError
from carbotek.ooxml.fragments import fragment_scope
from carbotek.ooxml.xml import serialize_xml
from carbotek.package import TemplatePart


def test_bytes_fragment_search_splice_and_materialize() -> None:
    with fragment_scope():
        root = etree.Element("root")
        placeholder = rendered_placeholder("<t>Привет\ue001</t>".encode(), '<copies label="ключ">', "</copies>")
        assert placeholder is not None
        root.append(placeholder)
        part = TemplatePart.from_tree("document.xml", root)
        assert part.contains("Привет")
        assert part.contains("ключ")
        assert not part.contains("ключ", attributes=False)
        assert not part.contains(placeholder.get("ref", ""))
        expected = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n<root><t>\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82\xee\x80\x81</t></root>"
        assert serialize_xml(root) == expected
        assert serialize_xml(root) == expected
        copies = materialize(placeholder)
        assert len(copies) == 1
        assert copies[0].text == "Привет\ue001"


def test_scopes_isolate_byte_fragments_and_standalone_uses_cdata() -> None:
    outer = etree.Element("root")
    with fragment_scope():
        first = rendered_placeholder(b"<t>first</t>", "<copies>", "</copies>")
        assert first is not None
        outer.append(first)
        with fragment_scope():
            inner = etree.Element("root")
            second = rendered_placeholder(b"<t>second</t>", "<copies>", "</copies>")
            assert second is not None
            inner.append(second)
            assert b"<t>second</t>" in serialize_xml(inner)
            with pytest.raises(CorruptedTemplateError):
                serialize_xml(outer)
        assert b"<t>first</t>" in serialize_xml(outer)
        with pytest.raises(CorruptedTemplateError):
            serialize_xml(inner)
    with pytest.raises(CorruptedTemplateError):
        serialize_xml(outer)

    standalone = etree.Element("root")
    placeholder = rendered_placeholder(b"<t>standalone</t>", "<copies>", "</copies>")
    assert placeholder is not None
    standalone.append(placeholder)
    assert b"<t>standalone</t>" in serialize_xml(standalone)


@pytest.mark.skipif(not _native_adapter.native_available(), reason="the native extension is disabled")
def test_mixed_section_evaluates_each_slot_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def evaluate(row: object) -> str:
        calls.append("called")
        return "Привет"

    section = StringSection(["<t>", evaluate, "</t>"], "<copies>", "</copies>")
    assert section.render_xml_bytes([{}]) == "<t>Привет</t>".encode()
    assert calls == ["called"]
    monkeypatch.setattr(_native_adapter, "_native", None)
    assert section.render_xml_bytes([{}]) is None
    assert calls == ["called"]
    assert section.render_xml([{}]) == "<t>Привет</t>"
    assert calls == ["called", "called"]
