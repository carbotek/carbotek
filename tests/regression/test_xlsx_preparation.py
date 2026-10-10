"""Preparation preserves unchanged markup while validating/transcoding XML."""

import pytest

from carbotek.errors import CorruptedTemplateError
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.xlsx.prepare import prepare_workbook


def prepare(content: bytes) -> bytes:
    name = "xl/workbook.xml"
    package = TemplatePackage.create({name: TemplatePart(name, content, True)}, (name,), "xlsx")
    prepared, _ = prepare_workbook(package)
    return prepared.parts[name].content


def test_utf8_xml_keeps_original_declaration_and_empty_element_spelling() -> None:
    content = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook><sheets></sheets></workbook>'
    assert prepare(content) == content


def test_non_utf8_xml_is_transcoded_before_marker_preparation() -> None:
    content = '<?xml version="1.0" encoding="ISO-8859-1"?><workbook><name>caf\u00e9</name></workbook>'.encode("latin-1")
    assert "caf\u00e9" in prepare(content).decode("utf-8")
    assert b"ISO-8859-1" not in prepare(content)


def test_unchanged_utf8_xml_still_rejects_dtd() -> None:
    with pytest.raises(CorruptedTemplateError):
        prepare(b'<!DOCTYPE workbook [<!ENTITY x "bad">]><workbook/>')
