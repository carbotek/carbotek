//! Optional native accelerators of carbotek (`carbotek._native`).
//!
//! The Python renderer stays the reference. Every entry point either reproduces the Python result
//! for the exact subset it supports or returns `None`, and the caller then takes the Python path.
//! Nothing keeps state between calls, so the module is safe on free-threaded Python.

use std::fmt::Write as _;

use base64::Engine as _;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};

/// Stands for an emptied text until the copies are parsed (`_EMPTY` in carbotek/docx/strings.py).
const EMPTY: &str = "<!--ct-empty-->";

/// Operation codes of a slot part, mirrored by `carbotek._native_adapter` (checked by a test).
const OP_LITERAL: u8 = 0;
const OP_FIELD: u8 = 1;
const OP_ISO_DATE: u8 = 2;
const OP_RU_NUMBER: u8 = 3;
const OP_IF_EQUAL: u8 = 4;
const OP_CENTS: u8 = 5;
const OPERATIONS: [(&str, u8); 6] = [
    ("literal", OP_LITERAL),
    ("field", OP_FIELD),
    ("iso_date", OP_ISO_DATE),
    ("ru_number", OP_RU_NUMBER),
    ("if_equal", OP_IF_EQUAL),
    ("cents", OP_CENTS),
];

/// (operation, literal text or field name, ifEQ value, shown when equal, shown otherwise)
type Part = (u8, String, String, String, String);
type Slot = (usize, Vec<Part>);

/// One part of the text of a slot.
enum Piece {
    Literal(String),
    /// A field of the loop item, interned once for the dict lookups.
    Value {
        field: Py<PyString>,
        format: Format,
    },
}

/// The marker chains reproduced natively; arguments arrive decoded by the Python compiler.
enum Format {
    /// `{d.x}`, printed like `_printable` in carbotek/docx/renderer.py.
    Plain,
    /// `formatD(DD.MM.YYYY)` of a calendar `YYYY-MM-DD` string.
    IsoDate,
    /// `formatN(2)` with `lang` ru.
    RuNumber,
    /// `ifEQ(expected):show(shown):elseShow(otherwise)`.
    IfEqual {
        expected: String,
        shown: String,
        otherwise: String,
    },
    /// `digitCurrency` of an amount in whole kopecks.
    Cents,
}

impl Piece {
    fn compile(py: Python<'_>, part: Part) -> PyResult<Self> {
        let (operation, text, expected, shown, otherwise) = part;
        let format = match operation {
            OP_LITERAL => return Ok(Self::Literal(text)),
            OP_FIELD => Format::Plain,
            OP_ISO_DATE => Format::IsoDate,
            OP_RU_NUMBER => Format::RuNumber,
            OP_IF_EQUAL => Format::IfEqual {
                expected,
                shown,
                otherwise,
            },
            OP_CENTS => Format::Cents,
            _ => return Err(PyValueError::new_err("Invalid section slot operation")),
        };
        Ok(Self::Value {
            field: PyString::intern(py, &text).unbind(),
            format,
        })
    }

    /// Appends this part for `row`; `false` asks for the Python renderer.
    fn write(&self, row: &Bound<'_, PyDict>, cell: &mut String) -> PyResult<bool> {
        let (field, format) = match self {
            Self::Literal(text) => {
                cell.push_str(text);
                return Ok(true);
            }
            Self::Value { field, format } => (field, format),
        };
        match row.get_item(field.bind(row.py()))? {
            Some(value) if !value.is_none() => format.write(cell, &value),
            value => {
                format.write_null(cell, value.is_some());
                Ok(true)
            }
        }
    }
}

impl Format {
    fn write(&self, cell: &mut String, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(match self {
            Self::Plain => write_value(cell, value)?,
            Self::IsoDate => exact_str(value).is_some_and(|raw| write_iso_date(cell, raw)),
            Self::RuNumber => {
                exact_number(value).is_some_and(|number| write_ru_number(cell, number))
            }
            Self::Cents => exact_number(value).is_some_and(|number| write_cents(cell, number)),
            Self::IfEqual {
                expected,
                shown,
                otherwise,
            } => exact_str(value).is_some_and(|raw| {
                cell.push_str(if raw == expected { shown } else { otherwise });
                true
            }),
        })
    }

    /// Missing and null print nothing but have different JS strings in conditions.
    fn write_null(&self, cell: &mut String, present: bool) {
        if let Self::IfEqual {
            expected,
            shown,
            otherwise,
        } = self
        {
            let text = if present { "null" } else { "undefined" };
            cell.push_str(if expected == text { shown } else { otherwise });
        }
    }
}

#[pyclass(module = "carbotek._native", frozen)]
struct PlainSection {
    literals: Vec<String>,
    slots: Vec<(usize, Vec<Piece>)>,
    /// Estimated bytes of one rendered row, to size the output once.
    row_hint: usize,
}

#[pymethods]
impl PlainSection {
    #[new]
    fn new(py: Python<'_>, literals: Vec<String>, slots: Vec<Slot>) -> PyResult<Self> {
        let mut next = 0;
        let mut compiled = Vec::with_capacity(slots.len());
        for (index, parts) in slots {
            if index < next || index >= literals.len() || !literals[index].is_empty() {
                return Err(PyValueError::new_err("Invalid section slot layout"));
            }
            let pieces = parts
                .into_iter()
                .map(|part| Piece::compile(py, part))
                .collect::<PyResult<Vec<_>>>()?;
            compiled.push((index, pieces));
            next = index + 1;
        }
        let row_hint = literals.iter().map(String::len).sum::<usize>() + 16 * compiled.len();
        Ok(Self {
            literals,
            slots: compiled,
            row_hint,
        })
    }

    /// The XML of all rows, or None to request the Python renderer for a value outside the subset.
    fn render(&self, rows: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        self.render_rows(rows)
    }

    /// The same XML as UTF-8, for render-owned fragments.
    fn render_bytes<'py>(&self, rows: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        Ok(self
            .render_rows(rows)?
            .map(|xml| PyBytes::new(rows.py(), xml.as_bytes())))
    }
}

impl PlainSection {
    fn render_rows(&self, rows: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        // A list subclass may override iteration: the Python path honours it.
        let Ok(rows) = rows.cast_exact::<PyList>() else {
            return Ok(None);
        };
        let mut output = String::with_capacity(self.row_hint.saturating_mul(rows.len()));
        let mut cell = String::new();
        for row in rows.iter() {
            let Ok(row) = row.cast_exact::<PyDict>() else {
                return Ok(None);
            };
            let mut cursor = 0;
            for (index, pieces) in &self.slots {
                for literal in &self.literals[cursor..*index] {
                    output.push_str(literal);
                }
                cell.clear();
                for piece in pieces {
                    if !piece.write(row, &mut cell)? {
                        return Ok(None);
                    }
                }
                write_text(&mut output, &cell);
                cursor = index + 1;
            }
            for literal in &self.literals[cursor..] {
                output.push_str(literal);
            }
        }
        Ok(Some(output))
    }
}

fn exact_str<'a>(value: &'a Bound<'_, PyAny>) -> Option<&'a str> {
    // A lone surrogate cannot be UTF-8: the Python path removes it.
    value.cast_exact::<PyString>().ok()?.to_str().ok()
}

/// An exact `float` or `int` (not a bool or a subclass) as a double, like `float(value)`.
fn exact_number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if let Ok(number) = value.cast_exact::<PyFloat>() {
        Some(number.value())
    } else if value.is_exact_instance_of::<PyInt>() {
        value.extract::<f64>().ok()
    } else {
        None
    }
}

/// `_printable`: strings, booleans, integers and floats; anything else goes to Python.
fn write_value(cell: &mut String, value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if let Some(raw) = exact_str(value) {
        cell.push_str(raw);
    } else if value.is_exact_instance_of::<PyString>() {
        return Ok(false);
    } else if let Ok(flag) = value.cast_exact::<PyBool>() {
        cell.push_str(if flag.is_true() { "true" } else { "false" });
    } else if value.is_exact_instance_of::<PyInt>() {
        if let Ok(number) = value.extract::<i64>() {
            let _ = write!(cell, "{number}");
        } else {
            cell.push_str(value.str()?.to_str()?);
        }
    } else if let Ok(number) = value.cast_exact::<PyFloat>() {
        write_js_number(cell, number.value());
    } else {
        return Ok(false);
    }
    Ok(true)
}

/// `String(x)` of a JS number (ECMA-262 `Number::toString`): the shortest round-trip digits, in
/// exponent form below 1e-6 and from 1e21 (`js_float_string` in formatters/jsnumber.py).
fn write_js_number(output: &mut String, number: f64) {
    if number.is_nan() {
        output.push_str("NaN");
        return;
    }
    if number == 0.0 {
        output.push('0'); // +0 and -0
        return;
    }
    if number < 0.0 {
        output.push('-');
    }
    let number = number.abs();
    if number.is_infinite() {
        output.push_str("Infinity");
        return;
    }
    let (digits, point) = shortest_decimal(number);
    let digits = digits.as_str();
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    if count <= point && point <= 21 {
        output.push_str(digits);
        output.extend(std::iter::repeat_n(
            '0',
            usize::try_from(point - count).unwrap_or(0),
        ));
    } else if 0 < point && point <= 21 {
        let split = usize::try_from(point).unwrap_or(0);
        output.push_str(&digits[..split]);
        output.push('.');
        output.push_str(&digits[split..]);
    } else if -6 < point && point <= 0 {
        output.push_str("0.");
        output.extend(std::iter::repeat_n(
            '0',
            usize::try_from(-point).unwrap_or(0),
        ));
        output.push_str(digits);
    } else {
        let power = point - 1;
        output.push_str(&digits[..1]);
        if count > 1 {
            output.push('.');
            output.push_str(&digits[1..]);
        }
        let _ = write!(
            output,
            "e{}{}",
            if power >= 0 { '+' } else { '-' },
            power.abs()
        );
    }
}

fn write_iso_date(output: &mut String, raw: &str) -> bool {
    let bytes = raw.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes
            .iter()
            .enumerate()
            .any(|(i, byte)| i != 4 && i != 7 && !byte.is_ascii_digit())
    {
        return false;
    }
    let year = raw[0..4].parse::<u32>().unwrap_or(0);
    let month = raw[5..7].parse::<u32>().unwrap_or(0);
    let day = raw[8..10].parse::<u32>().unwrap_or(0);
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    if year == 0 || day == 0 || day > max_day {
        return false;
    }
    output.push_str(&raw[8..10]);
    output.push('.');
    output.push_str(&raw[5..7]);
    output.push('.');
    output.push_str(&raw[0..4]);
    true
}

/// `formatN(2)` with lang ru, as carbone's `round()` computes it: `Math.round(Number(String(x) +
/// "e+2"))`, a shift of the decimal string, then `toFixed(2)` of the hundredths.
#[allow(clippy::cast_possible_truncation)] // |hundredths| < 1e14
fn write_ru_number(output: &mut String, number: f64) -> bool {
    if !number.is_finite() || number.abs() >= 1.0e12 {
        return false;
    }
    if number.abs() < 1.0e-6 {
        // Zero, or String(x) has an exponent and round() shifts it to a rounded zero.
        write_amount(output, 0);
        return true;
    }
    // String(x) + "e+2": the shortest digits with the decimal point moved two places.
    let (digits, point) = shortest_decimal(number.abs());
    let power = point - i32::try_from(digits.len()).unwrap_or(0) + 2;
    let sign = if number < 0.0 { "-" } else { "" };
    let Ok(shifted) = format!("{sign}{digits}e{power}").parse::<f64>() else {
        return false;
    };
    write_amount(output, js_math_round(shifted) as i64);
    true
}

/// The shortest round-trip digits of a positive finite number, ties to even like JS and Python
/// `repr`, and the decimal point position: `number = 0.digits * 10^point`.
fn shortest_decimal(number: f64) -> (String, i32) {
    let mut buffer = ryu::Buffer::new();
    let text = buffer.format_finite(number); // "1.5e-7", "0.001234", "1234.0"
    let (mantissa, exponent) = text.split_once('e').unwrap_or((text, "0"));
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let mut digits = format!("{whole}{fraction}");
    let leading = digits.len() - digits.trim_start_matches('0').len();
    let point = i32::try_from(whole.len()).unwrap_or(0) + exponent.parse::<i32>().unwrap_or(0)
        - i32::try_from(leading).unwrap_or(0);
    digits.drain(..leading);
    digits.truncate(digits.trim_end_matches('0').len());
    (digits, point)
}

/// `Math.round`: the nearest integer, ties toward +infinity (computed exactly).
fn js_math_round(value: f64) -> f64 {
    let floor = value.floor();
    if value - floor >= 0.5 {
        floor + 1.0
    } else {
        floor
    }
}

/// `digitCurrency` of an amount of whole kopecks, as `round_half_up` and `_to_fixed` in
/// formatters/ru.py print it (their decimal string shift gives the same kopecks below 1e12).
#[allow(clippy::cast_possible_truncation)] // |x * 100| < 1e14
fn write_cents(output: &mut String, number: f64) -> bool {
    if !number.is_finite() || number.abs() >= 1.0e12 {
        return false;
    }
    if number != 0.0 && number.abs() < 1.0e-6 {
        // String(x) has an exponent: roundHalfUp keeps the value and a negative one prints "-0,00".
        return false;
    }
    let scaled = number * 100.0;
    let nearest = scaled.round();
    let cents = if (scaled - nearest).abs() <= 1.0e-7 {
        // Binary noise of a computed amount (0.1 + 0.2): the shifted decimal rounds to it too.
        nearest as i64
    } else if let Some(cents) = decimal_cents(number) {
        // Large amounts: x * 100 is inexact, the shortest decimal form is not.
        cents
    } else {
        return false;
    };
    write_amount(output, cents);
    true
}

/// Hundredths of `number` when its shortest round-trip decimal form has at most two decimals.
fn decimal_cents(number: f64) -> Option<i64> {
    if number == 0.0 {
        return Some(0);
    }
    let (digits, point) = shortest_decimal(number.abs());
    let decimals = i32::try_from(digits.len()).ok()? - point;
    if decimals > 2 {
        return None;
    }
    let scale = 10_i64.checked_pow(u32::try_from(2 - decimals).ok()?)?;
    let cents = digits.parse::<i64>().ok()?.checked_mul(scale)?;
    Some(if number < 0.0 { -cents } else { cents })
}

/// "-1 234,56" from -123456 hundredths: space groups, comma decimals.
fn write_amount(output: &mut String, cents: i64) {
    if cents < 0 {
        output.push('-');
    }
    let absolute = cents.unsigned_abs();
    let whole = (absolute / 100).to_string();
    for (index, digit) in whole.chars().enumerate() {
        if index != 0 && (whole.len() - index).is_multiple_of(3) {
            output.push(' ');
        }
        output.push(digit);
    }
    let _ = write!(output, ",{:02}", absolute % 100);
}

/// Escaped exactly as lxml serializes element text; the empty marker when nothing is left.
fn write_text(output: &mut String, input: &str) {
    if input.is_empty() {
        output.push_str(EMPTY);
        return;
    }
    let before = output.len();
    for character in input.chars() {
        match character {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '\r' => output.push_str("&#13;"),
            '\u{0000}'..='\u{0008}'
            | '\u{000b}'
            | '\u{000c}'
            | '\u{000e}'..='\u{001f}'
            | '\u{fffe}'
            | '\u{ffff}' => {}
            _ => output.push(character),
        }
    }
    if output.len() == before {
        output.push_str(EMPTY);
    }
}

/// Fast path of `digitCurrency(value)` with two decimal places.
/// None asks the Python formatter to keep all remaining JS-compatible cases.
#[pyfunction]
fn digit_currency_cents(value: &Bound<'_, PyAny>) -> Option<String> {
    let number = exact_number(value)?;
    let mut output = String::new();
    write_cents(&mut output, number).then_some(output)
}

/// Join already evaluated section pieces into one UTF-8 buffer, copying each piece once.
#[pyfunction]
fn join_utf8<'py>(parts: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyBytes>> {
    let items: Vec<Bound<'py, PyAny>> = parts.iter().collect();
    let mut chunks: Vec<&[u8]> = Vec::with_capacity(items.len());
    let mut total = 0;
    for item in &items {
        let chunk = if let Ok(bytes) = item.cast_exact::<PyBytes>() {
            bytes.as_bytes()
        } else if let Ok(text) = item.cast_exact::<PyString>() {
            text.to_str()?.as_bytes()
        } else {
            return Err(PyTypeError::new_err("Section pieces must be str or bytes"));
        };
        total += chunk.len();
        chunks.push(chunk);
    }
    PyBytes::new_with(parts.py(), total, |buffer| {
        let mut offset = 0;
        for chunk in &chunks {
            buffer[offset..offset + chunk.len()].copy_from_slice(chunk);
            offset += chunk.len();
        }
        Ok(())
    })
}

/// Canonical data URIs only; None leaves unusual headers and malformed data to Python.
#[pyfunction]
fn decode_data_uri<'py>(value: &Bound<'py, PyAny>) -> Option<Bound<'py, PyBytes>> {
    let text = exact_str(value)?;
    let encoded = [
        "data:image/png;base64,",
        "data:image/jpeg;base64,",
        "data:image/gif;base64,",
        "data:image/bmp;base64,",
        "data:image/svg+xml;base64,",
    ]
    .iter()
    .find_map(|prefix| text.strip_prefix(prefix))?;
    if encoded.len() % 4 != 0 || !encoded.is_ascii() {
        return None;
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(encoded.as_bytes())
        .ok()?;
    Some(PyBytes::new(value.py(), &decoded))
}

#[pymodule(gil_used = false)]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("API_VERSION", 2)?;
    module.add("PACKAGE_VERSION", env!("CARGO_PKG_VERSION"))?;
    module.add_class::<PlainSection>()?;
    module.add_function(wrap_pyfunction!(digit_currency_cents, module)?)?;
    module.add_function(wrap_pyfunction!(join_utf8, module)?)?;
    module.add_function(wrap_pyfunction!(decode_data_uri, module)?)?;
    let operations = PyDict::new(module.py());
    for (name, code) in OPERATIONS {
        operations.set_item(name, code)?;
    }
    module.add("OPERATIONS", operations)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(input: &str) -> String {
        let mut output = String::new();
        write_text(&mut output, input);
        output
    }

    #[test]
    fn text_is_escaped_like_lxml_and_controls_are_dropped() {
        assert_eq!(text("<a & b>\r\n\t"), "&lt;a &amp; b&gt;&#13;\n\t");
        assert_eq!(text(""), EMPTY);
        assert_eq!(text("\u{1}\u{fffe}"), EMPTY);
        assert_eq!(text("a\u{b}b\u{ffff}"), "ab");
    }

    #[test]
    fn iso_dates_are_validated_calendar_dates() {
        let mut output = String::new();
        assert!(write_iso_date(&mut output, "2024-02-29"));
        assert_eq!(output, "29.02.2024");
        for invalid in [
            "2023-02-29",
            "0000-01-01",
            "2024-13-01",
            "2024-00-10",
            "2024-1-01",
            "2024/01/01",
        ] {
            assert!(!write_iso_date(&mut String::new(), invalid), "{invalid}");
        }
    }

    #[test]
    fn amounts_group_thousands_with_spaces() {
        for (cents, expected) in [
            (0, "0,00"),
            (5, "0,05"),
            (-5, "-0,05"),
            (100, "1,00"),
            (123_456, "1 234,56"),
            (-123_456_789, "-1 234 567,89"),
            (99_999_999_999_999, "999 999 999 999,99"),
        ] {
            let mut output = String::new();
            write_amount(&mut output, cents);
            assert_eq!(output, expected);
        }
    }

    #[test]
    fn cents_accept_only_whole_kopecks() {
        for (number, expected) in [
            (1234.5, "1 234,50"),
            (1.15, "1,15"),
            (-0.0, "0,00"),
            (-0.01, "-0,01"),
            (30_000.0, "30 000,00"),
        ] {
            let mut output = String::new();
            assert!(write_cents(&mut output, number), "{number}");
            assert_eq!(output, expected);
        }
        for number in [
            1.234,
            0.005,
            1.0e12,
            -1.0e12,
            f64::NAN,
            f64::INFINITY,
            -1.0e-10,
            1.0e-10,
            -5.0e-324,
        ] {
            assert!(!write_cents(&mut String::new(), number), "{number}");
        }
    }

    #[test]
    fn large_amounts_use_their_shortest_decimal_form() {
        for (number, expected) in [
            (98_765_432_109.87, "98 765 432 109,87"),
            (-123_456_789_012.3, "-123 456 789 012,30"),
            (999_999_999_999.99, "999 999 999 999,99"),
            (0.1 + 0.2, "0,30"),
        ] {
            let mut output = String::new();
            assert!(write_cents(&mut output, number), "{number}");
            assert_eq!(output, expected);
        }
        assert_eq!(decimal_cents(12_345_678_901.234), None);
        assert_eq!(decimal_cents(-0.0), Some(0));
    }

    #[test]
    fn ru_numbers_shift_the_decimal_string_like_js() {
        for (number, expected) in [
            (1234.5, "1 234,50"),
            (0.125, "0,13"),
            (-0.125, "-0,12"),
            (1.005, "1,01"),
            (0.285, "0,29"),
            (1_188_151.535, "1 188 151,54"),
            (-8_781_202.675, "-8 781 202,67"),
            (-0.004, "0,00"),
            (-0.006, "-0,01"),
            (-0.0, "0,00"),
            (1.0e-7, "0,00"),
            (-1.0e-7, "0,00"),
            (999_999_999_999.99, "999 999 999 999,99"),
        ] {
            let mut output = String::new();
            assert!(write_ru_number(&mut output, number), "{number}");
            assert_eq!(output, expected, "{number}");
        }
        assert!(!write_ru_number(&mut String::new(), 1.0e12));
        assert!(!write_ru_number(&mut String::new(), f64::NAN));
    }

    #[test]
    fn numbers_print_like_js_string() {
        for (number, expected) in [
            (1.0, "1"),
            (-0.0, "0"),
            (0.1, "0.1"),
            (100.0, "100"),
            (1.0e-5, "0.00001"),
            (1.5e-5, "0.000015"),
            (0.000_001, "0.000001"),
            (1.0e-7, "1e-7"),
            (1.5e-7, "1.5e-7"),
            (-2.5e-8, "-2.5e-8"),
            (1.0e16, "10000000000000000"),
            (1.5e17, "150000000000000000"),
            (123_456_789_012_345_680_000.0, "123456789012345680000"),
            (1.0e21, "1e+21"),
            (1.234e22, "1.234e+22"),
            // Exact decimal ties of the shortest digits: JS and Python repr keep the even digit.
            (17_463_905_152_460.812, "17463905152460.812"),
            (1_297_319_342_356_239.2, "1297319342356239.2"),
            (5.0e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ] {
            let mut output = String::new();
            write_js_number(&mut output, number);
            assert_eq!(output, expected, "{number:e}");
        }
    }
}
