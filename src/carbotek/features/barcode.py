# pyright: strict, reportPrivateUsage=false
"""PNG barcode rendering for Word drawing placeholders."""

from io import BytesIO
from typing import Any

lazy import barcode  # pyright: ignore[reportMissingTypeStubs] - python-barcode ships no stubs
lazy import segno
lazy from barcode.errors import BarcodeError  # pyright: ignore[reportMissingTypeStubs]
lazy from barcode.writer import ImageWriter  # pyright: ignore[reportMissingTypeStubs] - python-barcode ships no stubs
lazy from PIL import Image, ImageDraw, ImageFont

from carbotek.docx.session import CURRENT_RENDER
from carbotek.errors import FormatterError, MarkerResolutionError

lazy from ._barcode_writer import BarcodeImageWriter

_LINEAR_TYPES = {
    "code128": "code128",
    "code39": "code39",
    "ean13": "ean13",
    "ean8": "ean8",
    "upca": "upca",
    "isbn": "isbn",
    "issn": "issn",
    "itf": "itf",
    "interleaved2of5": "itf",
    "ean14": "ean14",
    "gs1-128": "gs1_128",
    "codabar": "codabar",
    "pzn": "pzn",
}
SUPPORTED_BARCODES = frozenset({"qrcode", "microqrcode", "ean2", "ean5", "code39ext", *_LINEAR_TYPES})
_ROTATION = {"N": 0, "R": -90, "I": 180, "L": 90}
_EAN_L = ("0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011")
_EAN_G = ("0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111")
_EAN5_PARITY = ("GGLLL", "GLGLL", "GLLGL", "GLLLG", "LGGLL", "LLGGL", "LLLGG", "LGLGL", "LGLLG", "LLGLG")


def render_barcode(value: Any, arguments: tuple[str, ...]) -> tuple[bytes, str]:
    session = CURRENT_RENDER.get()
    key = ("barcode", str(value), arguments)
    if session is not None and key in session.assets:
        return session.assets[key]
    result = _render_barcode(value, arguments)
    if session is not None:
        session.assets[key] = result
    return result


def _render_barcode(value: Any, arguments: tuple[str, ...]) -> tuple[bytes, str]:
    """Render a supported Carbotek image barcode as PNG bytes."""
    if not arguments:
        raise MarkerResolutionError("barcode requires a type")
    kind = arguments[0].strip("\"'").lower()
    if kind not in SUPPORTED_BARCODES:
        available = ", ".join(sorted(SUPPORTED_BARCODES))
        raise MarkerResolutionError(f'Unsupported barcode type "{kind}"; available: {available}')
    options = _options(arguments[1:])
    content = str(value) if value is not None else ""
    if not content:
        raise MarkerResolutionError("barcode value must not be empty")
    if len(content.encode("utf-8")) > (4096 if kind in {"qrcode", "microqrcode"} else 512):
        raise MarkerResolutionError("Barcode generation error: value is too long")
    try:
        if kind in {"qrcode", "microqrcode"}:
            image = _qr(content, options, micro=kind == "microqrcode")
        elif kind in {"ean2", "ean5"}:
            image = _ean_addon(content, kind, options)
        elif kind == "code39ext":
            image = _code39_extended_image(content, options)
        else:
            image = _linear(content, kind, options)
        if options.get("rotate") in _ROTATION and options["rotate"] != "N":
            with Image.open(BytesIO(image)) as bitmap:
                rotated = bitmap.rotate(_ROTATION[options["rotate"]], expand=True)
                output = BytesIO()
                rotated.save(output, format="PNG")
                image = output.getvalue()
        with Image.open(BytesIO(image)) as bitmap:
            if bitmap.width * bitmap.height > 25_000_000:
                raise MarkerResolutionError("Barcode generation error: barcode exceeds 25000000 pixels")
        return image, "image/png"
    except MarkerResolutionError:
        raise
    except (BarcodeError, ValueError, TypeError, KeyError) as error:
        raise FormatterError(f"Barcode generation error: {error}") from error


def _options(arguments: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for argument in arguments:
        key, separator, value = argument.strip("\"'").partition(":")
        if not separator:
            continue
        key, value = key.lower(), value.strip("\"'")
        if key in {"barcolor", "backgroundcolor", "textcolor"}:
            color = value.removeprefix("#")
            if len(color) == 6 and all(digit in "0123456789abcdefABCDEF" for digit in color):
                parsed[key] = f"#{color}"
        elif key in {"scale", "textsize", "width", "height"}:
            limit = 10 if key == "scale" else 100 if key == "textsize" else 1000
            if value.isdecimal() and 0 < int(value) <= limit:
                parsed[key] = value
        elif (key == "rotate" and value.upper() in _ROTATION) or (
            key == "eclevel" and value.upper() in {"L", "M", "Q", "H"}
        ):
            parsed[key] = value.upper()
        elif (
            (key == "textxalign" and value in {"left", "center", "right", "justify"})
            or (key == "textyalign" and value in {"below", "center", "above"})
            or (key == "includetext" and value in {"true", "false"})
        ):
            parsed[key] = value
    return parsed


def _qr(content: str, options: dict[str, str], *, micro: bool = False) -> bytes:
    output = BytesIO()
    code = segno.make(content, error=options.get("eclevel") if micro else options.get("eclevel", "M"), micro=micro)
    modules = code.symbol_size(border=0)[0]
    scale = int(options.get("scale", "3"))

    def module_pixels(dimension: str) -> int:
        if dimension not in options:
            return 2 * scale
        requested_pixels = int(options[dimension]) * 72 / 25.4 * scale
        return max(1, int(requested_pixels / modules))

    width_pixels = module_pixels("width")
    height_pixels = module_pixels("height")
    code.save(
        output,
        kind="png",
        scale=1,
        border=0,
        dark=options.get("barcolor", "#000000"),
        light=options.get("backgroundcolor", "#ffffff"),
    )
    with Image.open(BytesIO(output.getvalue())) as bitmap:
        stretched = bitmap.resize(  # pyright: ignore[reportUnknownMemberType]
            (modules * width_pixels, modules * height_pixels), Image.Resampling.NEAREST
        )
        result = BytesIO()
        stretched.save(result, format="PNG")
    return result.getvalue()


def _ean_addon(content: str, kind: str, options: dict[str, str]) -> bytes:
    digits = 2 if kind == "ean2" else 5
    if len(content) != digits or not content.isascii() or not content.isdecimal():
        raise MarkerResolutionError(f"Barcode generation error: {kind} requires exactly {digits} digits")
    if digits == 2:
        parity = ("LL", "LG", "GL", "GG")[int(content) % 4]
    else:
        checksum = (
            3 * sum(int(content[index]) for index in (0, 2, 4)) + 9 * sum(int(content[index]) for index in (1, 3))
        ) % 10
        parity = _EAN5_PARITY[checksum]
    modules = "1011" + "01".join(
        (_EAN_L if parity[index] == "L" else _EAN_G)[int(digit)] for index, digit in enumerate(content)
    )
    scale = int(options.get("scale", "3"))
    module_width = scale
    if "width" in options:
        requested_pixels = int(options["width"]) * 72 / 25.4 * scale
        module_width = max(1, int(requested_pixels / len(modules)))
    bar_height = round(50.8 * scale) if "height" not in options else round(int(options["height"]) * 72 / 25.4 * scale)
    include_text = options.get("includetext", "true") == "true"
    requested_text_size = int(options.get("textsize", "12"))
    text_size = round(requested_text_size * scale * 2 / 3) if include_text else 0
    text_height = round((2 * requested_text_size + 4) * scale / 3) if include_text else 0
    image = Image.new(
        "RGB",
        (len(modules) * module_width + scale % 2, bar_height + text_height),
        options.get("backgroundcolor", "#ffffff"),
    )
    drawing = ImageDraw.Draw(image)
    bar_color = options.get("barcolor", "#000000")
    for index, module in enumerate(modules):
        if module == "1":
            left = index * module_width
            drawing.rectangle((left, 0, left + module_width - 1, bar_height - 1), fill=bar_color)
    if include_text:
        font = ImageFont.truetype(ImageWriter().font_path, text_size)
        text_color = options.get("textcolor", bar_color)
        text_width = drawing.textlength(content, font=font)
        alignment = options.get("textxalign", "center")
        left = (
            0
            if alignment == "left"
            else image.width - text_width
            if alignment == "right"
            else (image.width - text_width) / 2
        )
        drawing.text((left, bar_height), content, font=font, fill=text_color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _linear(content: str, kind: str, options: dict[str, str]) -> bytes:
    output = BytesIO()
    barcode_class = barcode.get_barcode_class(_LINEAR_TYPES[kind])
    code = barcode_class(
        content,
        writer=BarcodeImageWriter(
            options.get("textcolor", options.get("barcolor", "#000000")),
            options.get("textxalign", "center"),
            options.get("textyalign", "below"),
        ),
    )
    writer_options: dict[str, Any] = {
        "foreground": options.get("barcolor", "#000000"),
        "background": options.get("backgroundcolor", "#ffffff"),
        "write_text": options.get("includetext", "true") == "true",
        "module_width": 0.2 * int(options.get("scale", "3")) / 3,
        "module_height": float(options.get("height", "15")),
    }
    if "width" in options:
        modules = len(code.build()[0])
        requested_pixels = int(options["width"]) * 72 / 25.4 * int(options.get("scale", "3"))
        pixels_per_module = max(1, int(requested_pixels / modules))
        writer_options["module_width"] = pixels_per_module * 25.4 / 300 + 1e-7
        writer_options["quiet_zone"] = 0
    if "textsize" in options:
        writer_options["font_size"] = int(options["textsize"])
    code.write(output, writer_options)  # pyright: ignore[reportUnknownMemberType] - untyped library
    return output.getvalue()


def _code39_extended_image(content: str, options: dict[str, str]) -> bytes:
    encoded = _code39_extended(content)
    code = barcode.get_barcode_class("code39")(encoded, add_checksum=False)
    modules = code.build()[0]
    scale = int(options.get("scale", "3"))
    module_width = scale
    if "width" in options:
        requested_pixels = int(options["width"]) * 72 / 25.4 * scale
        module_width = max(1, int(requested_pixels / len(modules)))
    bar_height = round(72.3 * scale) if "height" not in options else round(int(options["height"]) * 72 / 25.4 * scale)
    include_text = options.get("includetext", "true") == "true"
    text_height = (
        round((8.3 if "textsize" not in options else int(options["textsize"]) * 2 / 3) * scale) if include_text else 0
    )
    image = Image.new(
        "RGB",
        (len(modules) * module_width + scale % 2, bar_height + text_height),
        options.get("backgroundcolor", "#ffffff"),
    )
    drawing = ImageDraw.Draw(image)
    bar_color = options.get("barcolor", "#000000")
    for index, module in enumerate(modules):
        if module == "1":
            left = index * module_width
            drawing.rectangle((left, 0, left + module_width - 1, bar_height - 1), fill=bar_color)
    if include_text:
        font = ImageFont.truetype(ImageWriter().font_path, max(1, text_height - scale))
        text_width = drawing.textlength(content, font=font)
        alignment = options.get("textxalign", "center")
        left = (
            0
            if alignment == "left"
            else image.width - text_width
            if alignment == "right"
            else (image.width - text_width) / 2
        )
        drawing.text((left, bar_height), content, font=font, fill=options.get("textcolor", bar_color))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _code39_extended(content: str) -> str:  # noqa: C901 - ASCII ranges follow the BWIPP table directly.
    result: list[str] = []
    for character in content:
        code = ord(character)
        if code > 127:
            raise MarkerResolutionError("Barcode generation error: code39ext accepts ASCII characters only")
        if code == 0:
            result.append("%U")
        elif code <= 26:
            result.append("$" + chr(code + 64))
        elif code <= 31:
            result.append("%" + chr(code + 38))
        elif code == 32:
            result.append(" ")
        elif code <= 44:
            result.append("/" + chr(code + 32))
        elif code in {45, 46}:
            result.append(character)
        elif code == 47:
            result.append("/O")
        elif code <= 57:
            result.append(character)
        elif code == 58:
            result.append("/Z")
        elif code <= 63:
            result.append("%" + chr(code + 11))
        elif code == 64:
            result.append("%V")
        elif code <= 90:
            result.append(character)
        elif code <= 95:
            result.append("%" + chr(code - 16))
        elif code == 96:
            result.append("%W")
        elif code <= 122:
            result.append("+" + chr(code - 32))
        else:
            result.append("%" + chr(code - 43))
    return "".join(result)
