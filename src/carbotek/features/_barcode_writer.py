# pyright: strict, reportPrivateUsage=false
"""python-barcode image writer with text colour and alignment, imported lazily with the library."""

from barcode.writer import (  # pyright: ignore[reportMissingTypeStubs] - python-barcode ships no stubs
    ImageWriter,
    mm2px,
)
from PIL import ImageFont


class BarcodeImageWriter(ImageWriter):
    def __init__(self, text_color: str, text_align: str = "center", text_y_align: str = "below") -> None:
        super().__init__()
        self.text_color = text_color
        self.text_align = text_align
        self.text_y_align = text_y_align

    def _paint_module(self, xpos: float, ypos: float, width: float, color: str) -> None:
        if self.text_y_align == "above" and self.text:
            ypos += self._text_space()
        x0 = mm2px(xpos, self.dpi)
        x1 = max(x0, mm2px(xpos + width, self.dpi) - 1)
        self._draw.rectangle(
            [(x0, mm2px(ypos, self.dpi)), (x1, mm2px(ypos + self.module_height, self.dpi))],
            outline=color,
            fill=color,
        )

    def _text_space(self) -> float:
        lines = max(1, len(self.text.splitlines()))
        return self.text_distance + self.font_size * 25.4 / 144 * lines + self.text_line_distance * (lines - 1)

    def _paint_text(self, xpos: float, ypos: float) -> None:
        foreground = self.foreground
        self.foreground = self.text_color
        try:
            if self.text_y_align == "above":
                ypos = self.margin_top + self.font_size * 25.4 / 72
            elif self.text_y_align == "center":
                ypos = self.margin_top + self.module_height / 2 + self.font_size * 25.4 / 144
            if self.text_align == "center":
                super()._paint_text(xpos, ypos)  # pyright: ignore[reportUnknownMemberType] - untyped library
            else:
                text = self.human or self.text
                font_size = int(self.font_size * self.dpi / 72)
                if font_size <= 0:
                    return
                font = ImageFont.truetype(self.font_path, font_size)
                if self.text_align == "justify":
                    x = self.quiet_zone * self.dpi / 25.4
                    width = self._image.width - 2 * x
                    for line in text.split("\n"):
                        glyph_widths = [font.getlength(char) for char in line]
                        total_width = sum(glyph_widths)
                        gap = (width - total_width) / (len(line) - 1) if len(line) > 1 and total_width < width else 0
                        cursor = x if gap else x + (width - total_width) / 2
                        for char, glyph_width in zip(line, glyph_widths, strict=True):
                            self._draw.text(
                                (cursor, ypos * self.dpi / 25.4), char, font=font, fill=self.foreground, anchor="ld"
                            )
                            cursor += glyph_width + gap
                        ypos += self.font_size * 25.4 / 144 + self.text_line_distance
                    return
                anchor = "ld" if self.text_align == "left" else "rd"
                x = self.quiet_zone * self.dpi / 25.4
                if self.text_align == "right":
                    x = self._image.width - x
                for line in text.split("\n"):
                    self._draw.text((x, ypos * self.dpi / 25.4), line, font=font, fill=self.foreground, anchor=anchor)
                    ypos += self.font_size * 25.4 / 144 + self.text_line_distance
        finally:
            self.foreground = foreground
