# carbotek

<p align="center">
  <a href="https://github.com/carbotek/carbotek/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/carbotek/carbotek/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/carbotek/"><img alt="PyPI" src="https://img.shields.io/pypi/v/carbotek?color=blue"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.15%2B-3776AB?logo=python&logoColor=white">
  <img alt="Rust" src="https://img.shields.io/badge/native-Rust%201.99-CE422B?logo=rust&logoColor=white">
  <a href="https://github.com/carbotek/carbotek/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-orange"></a>
  <br>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://microsoft.github.io/pyright/"><img alt="Checked with pyright" src="https://microsoft.github.io/pyright/img/pyright_badge.svg"></a>
  <a href="https://docs.gauge.sh"><img alt="Architecture: tach" src="https://img.shields.io/badge/architecture-tach-6C47FF"></a>
  <img alt="Typed" src="https://img.shields.io/badge/typing-typed-brightgreen">
  <img alt="Carbone syntax" src="https://img.shields.io/badge/Carbone%20syntax-5.14.4-2E7D32">
</p>

[English](https://github.com/carbotek/carbotek/blob/main/README.md) | **Русский**

Рендеринг DOCX- и XLSX-шаблонов с маркерами в стиле Carbone в DOCX, XLSX или PDF из Python. Поддерживаются `{d.name}`, циклы, условия, форматтеры, картинки, штрихкоды, HTML, ссылки и цвета. LibreOffice не нужен: PDF строится встроенной копией [dxpdf](https://github.com/nerdy-pro/dxpdf) с исправлениями вёрстки плавающих картинок (подпись поверх печати остаётся на месте, см. [CARBOTEK.md](https://github.com/carbotek/carbotek/blob/main/native/vendor/dxpdf/CARBOTEK.md)).

carbotek — Python-порт DOCX- и XLSX-движка [Carbone](https://github.com/carboneio/carbone) **v3.2.3**, последней версии под Apache License 2.0. Синтаксис шаблонов совместим с Carbone **5.14.4**. Поведение сверено с JS-движком: дифференциальный корпус и оракулы форматтеров.

> Лицензия **PolyForm Noncommercial 1.0.0**. Некоммерческое использование бесплатно, для коммерческого нужна отдельная лицензия (см. [LICENSE](https://github.com/carbotek/carbotek/blob/main/LICENSE)).

## Установка

```bash
pip install carbotek        # или: uv add carbotek
```

Нужен CPython 3.15+. Основная целевая среда — Linux в Docker (`python:3.15-slim-trixie`); CI прогоняет
тесты на выпущенном колесе внутри этого образа.

В Linux-колёсах (manylinux, x86_64 и aarch64, одно abi3-колесо для CPython 3.15+) есть необязательный нативный ускоритель (`carbotek._native`, Rust): он быстрее рендерит
частые табличные циклы и денежные форматтеры, а результат совпадает с путём на Python. Для
платформы без готового колеса он собирается из исходного архива, и тогда нужен Rust 1.87+.
`CARBOTEK_NATIVE=0` выключает ускоритель, `CARBOTEK_NATIVE=1` превращает его отсутствие в ошибку
импорта; переменная читается один раз, при импорте `carbotek`.

PDF-движок входит в Linux-колёса и от `CARBOTEK_NATIVE` не зависит. При установке из исходников
или универсального колеса встроенного движка нет: поставьте `carbotek[pdf-fallback]`, тогда PDF
строится пакетом `dxpdf` с PyPI, без исправлений вёрстки.

## Библиотека

```python
import json
import carbotek

template = open("Акт.docx", "rb").read()
data = json.load(open("data.json", encoding="utf-8"))

docx = carbotek.render(template, data, {"lang": "ru", "timezone": "Europe/Moscow"})
pdf = carbotek.render_pdf(template, data, image_dpi=220)   # только для DOCX-шаблонов

engine = carbotek.Engine(cache_size=16)   # свой кеш компиляции шаблонов
engine.render(template, data)
print(engine.metrics)                     # size, bytes, hits, misses, hit_rate
```

`carbotek.render` сам определяет тип шаблона: DOCX-шаблон даёт DOCX, XLSX-шаблон — XLSX. Функции модуля используют общий `Engine` по умолчанию.

### Опции

Опции — ключи `RenderOptions.create`:

| Ключ | Назначение |
|---|---|
| `lang` | Язык документа, по умолчанию `ru` |
| `timezone` | Часовой пояс IANA, по умолчанию `Europe/Moscow` |
| `complement` | Данные для `{c.*}` |
| `translations` | Словари для `{t(...)}` |
| `enum` | Перечисления для `convEnum` |
| `currency` | `{"source", "target", "rates"}` |
| `variable_str` | Переменные шаблона |
| `use_high_precision_arithmetic` | Арифметика повышенной точности |
| `export_formatted_values_as_text` | XLSX: форматированные числа оставить текстом |

### Русские форматтеры

carbotek добавляет форматтеры для российских деловых документов (в Carbone их нет):

| Форматтер (синонимы) | Пример | Результат |
|---|---|---|
| `digitCurrency` (`numCurrency`, `numberCurrency`, `digitalCurrency`) | `{d.a:digitCurrency}`, a = 1234567.5 | `1 234 567,50` |
| | `{d.a:digitCurrency(0)}` | `1 234 568` |
| `formatAmount` | `{d.a:formatAmount}`, a = 1234.5 | `1 234,50` |
| `wordNumber` (`words`, `numberToWords`) | `{d.a:wordNumber}`, a = 1234 | `одна тысяча двести тридцать четыре` |
| `wordCurrency` | `{d.a:wordCurrency}`, a = 1234.56 | `Одна тысяча двести тридцать четыре рубля 56 копеек` |
| `moneyFull` (`sumFull`) | `{d.a:moneyFull}`, a = 1234.56 | `1 234,56 (Одна тысяча двести тридцать четыре рубля 56 копеек)` |
| `procent` (`percent`) | `{d.a:procent}`, a = 20 | `20,00 %` |
| `dashIfZero` (`dashZero`) | `{d.a:dashIfZero}`, a = 0 | `—` |
| `plural` (`pluralize`) | `{d.n:plural('день','дня','дней')}`, n = 5 | `5 дней` |
| | `{d.n:plural('день','дня','дней', false)}`, n = 21 | `день` |
| `ruDate` | `{d.dt:ruDate}`, dt = `2026-10-11` | `«11» октября 2026` |
| `roundHalfUp` | `{d.a:roundHalfUp(1)}`, a = 2.25 | `2.3` |
| `parseAmount` | `{d.s:parseAmount}`, s = `1 234,50` | `1234.5` |
| `parseDate` | `{d.s:parseDate}`, s = `11.10.2026` | `2026-10-11` |
| `capitalize` (`firstUpper`) | `{d.s:capitalize}`, s = `иванов` | `Иванов` |

Денежные форматтеры принимают необязательную точность (по умолчанию 2) и округляют половину вверх.
`ruDate` берёт часовой пояс рендера или переданный аргументом.

### Ошибки

Ошибки шаблона и данных наследуют `carbotek.CarbotekError`. У каждой ошибки стабильный `code`:

| Код | Когда |
|---|---|
| `VALIDATION_ERROR` | Некорректный ввод |
| `TEMPLATE_ERROR` | Ошибка в шаблоне, маркере или форматтере |
| `FEATURE_UNAVAILABLE` | Функция не поддерживается |
| `CONVERTER_ERROR` | Ошибка конвертации в PDF |

Текст ошибок следует JS-движку там, где это применимо. Ошибки ввода и инфраструктуры имеют сообщения библиотеки.

### Картинки по URL и из файлов

По умолчанию загружаются только data URI. Остальные источники включаются явно:

```python
from carbotek.features.fetch import ImageSourceConfig

engine = carbotek.Engine(image_sources=ImageSourceConfig(
    allowlist=("cdn.example.com",),   # разрешённые хосты для http(s); приватные адреса запрещены
    file_root="/srv/images",          # file:// читаются только внутри этого каталога
    max_bytes=10_485_760,
    fetch_timeout_ms=5000,
    max_download_concurrency=4,
    total_timeout_ms=10000,
))
```

Без `image_sources` те же настройки берутся из переменных окружения:

- `CARBOTEK__IMAGE_URL_ALLOWLIST` — хосты через запятую;
- `CARBOTEK__IMAGE_FILE_ROOT`;
- `CARBOTEK__IMAGE_MAX_BYTES`;
- `CARBOTEK__IMAGE_FETCH_TIMEOUT_MS`;
- `CARBOTEK__IMAGE_MAX_DOWNLOAD_CONCURRENCY`;
- `CARBOTEK__IMAGE_TOTAL_TIMEOUT_MS`.

## XLSX

XLSX-шаблон рендерится в XLSX так же, как в Carbone. Поддерживаются:

- маркеры;
- циклы по строкам и ячейкам, в том числе вложенные;
- условия и условные блоки;
- `drop`/`keep` (`row`, `col`, `table`);
- агрегаты и переменные.

Книга готовится как в JS-движке:

- `calcChain.xml` удаляется;
- строки из `sharedStrings.xml` переносятся в ячейки;
- ячейка с одним маркером `:formatN` становится числовой. Оставить текст можно опцией `export_formatted_values_as_text` или маркером `{o.exportFormattedValuesAsText=true}`.

Маркеры работают в любой части книги: в именах листов, колонтитулах, именованных диапазонах, комментариях.

Что ещё нужно знать:

- **Картинки.** Маркер ставится в альтернативный текст картинки листа, например `{d.logo}` или `{d.code:barcode(ean13)}`.
- **Гиперссылки.** Адрес ссылки `https://d.site` превращается в `{d.site:validateURL}`.
- **Не пересчитываются** формулы, `dimension` и объединённые ячейки.
- **PDF из XLSX нет:** будет `FeatureUnavailable`.

## CLI

```bash
carbotek render шаблон.docx data.json -o out.docx
carbotek render шаблон.xlsx data.json                      # -> шаблон.out.xlsx
carbotek render шаблон.docx data.json --pdf -o out.pdf --image-dpi 300
carbotek bench  шаблон.docx data.json -n 50                # тёплый кеш: cold, mean, p50, p95, min, max, в секунду
carbotek bench  шаблон.docx data.json --no-cache --json
carbotek markers шаблон.docx                               # маркеры по частям после нормализации
```

Также работает `python -m carbotek ...`.

Общие флаги:

- `--lang`, `--timezone`;
- `--options '{"complement": {"company": "X"}}'`;
- `-` вместо файла данных — читать данные из stdin.

## Использование в веб-сервисе и очереди задач

API синхронный и нагружает CPU. На вход и выход идут байты. Ввода-вывода нет, кроме необязательной загрузки картинок.

- **Один `Engine` на процесс.** Переиспользуйте его, чтобы работал кеш скомпилированных шаблонов. `Engine` потокобезопасен.
- **Не вызывайте рендер прямо в event loop.**
  - В async-коде используйте `await asyncio.to_thread(engine.render, template, data, options)`.
  - Для очереди задач (taskiq, Celery) лучше всего масштабируется синхронная задача с воркерами-процессами, потому что рендер держит GIL.
- **`CarbotekError` отдавайте клиенту как 4xx** по `code`. Любое другое исключение — ошибка сервера.
- **Установите в контейнер шрифты шаблонов** или метрически совместимые замены (Carlito, Caladea): для PDF берутся системные шрифты.
  В образах `python:3.15-slim` также поставьте `libexpat1` (нужна встроенному fontconfig): `apt-get install -y libexpat1`.

```python
import asyncio
import carbotek

engine = carbotek.Engine(cache_size=64)

async def render_document(template: bytes, data: dict, pdf: bool) -> bytes:
    if pdf:
        return await asyncio.to_thread(engine.render_pdf, template, data)
    return await asyncio.to_thread(engine.render, template, data)
```

## Ограничения

- **Диаграммы ECharts не поддерживаются:** маркер `{d.x:chart}` вызывает `FeatureUnavailable`. Нативные диаграммы Word и Excel обновляются данными.
- **В PDF нет панели закладок и сведений о документе** (заголовок, автор).
- **Нативные OOXML-диаграммы в PDF не рисуются,** но их данные в DOCX обновляются.
- **WMF/EMF в PDF рисуются частично,** в лог пишется предупреждение.
- **Оглавление не пересчитывается:** номера страниц остаются из шаблона.
- **Чётный/нечётный разрыв раздела работает как `nextPage`.**

## Отличия от Carbone

Намеренные, задокументированные отличия от JavaScript-движка:

- Опция `{o.*}`, разбитая Word на несколько фрагментов, применяется (Carbone оставляет её в тексте).
- Объект или массив в маркере печатается по правилам JavaScript (`[object Object]`, `1,2`).
- Именованная сортировка вместе с повтором работает (Carbone не компилирует такой шаблон).
- Пустой ряд нативной диаграммы записывается как `ptCount=0`, как требует OOXML.
- Элементы без ключа именованной сортировки сохраняют исходный порядок.
- Если несколько картинок используют один маркер, каждая сохраняет размер своей рамки (Carbone
  применяет ко всем размер последней).
- Нативные колёса собираются только для Linux; на других платформах ставится pure-Python wheel, там он не тестируется.

Об уязвимостях сообщайте по правилам [SECURITY.md](https://github.com/carbotek/carbotek/blob/main/SECURITY.md).

## Лицензия

carbotek распространяется по [PolyForm Noncommercial License 1.0.0](https://github.com/carbotek/carbotek/blob/main/LICENSE). Copyright (c) 2026 Carbotek.

Проект основан на Carbone v3.2.3 (Copyright (c) CarboneIO SAS и участники, [Apache License 2.0](https://github.com/carbotek/carbotek/blob/main/licenses/Carbone-Apache-2.0.txt)): код переведён с JavaScript на Python и Rust и существенно переработан. carbotek не связан с CarboneIO SAS. См. [NOTICE](https://github.com/carbotek/carbotek/blob/main/NOTICE) и [THIRD_PARTY_LICENSES.md](https://github.com/carbotek/carbotek/blob/main/THIRD_PARTY_LICENSES.md).
