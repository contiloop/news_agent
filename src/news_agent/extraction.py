"""Pure AX and DOM article-body extraction rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any


class ReaderError(RuntimeError):
    """Raised when CUA cannot prove that one complete article was read."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "read_failed",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable


class AxExtractionError(ReaderError):
    """Raised only when a valid page snapshot cannot yield one AX body."""


_MIN_BODY_PARAGRAPHS = 2
_MIN_SINGLE_REUTERS_BODY_CHARACTERS = 200
_REUTERS_PARAGRAPH_TEST_ID_PREFIX = "paragraph-"
_TITLE_QUOTE_TRANSLATION = str.maketrans(
    {
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
    }
)
_AX_CREDIT_MAX_CHARACTERS = 100
_AX_RUN_BOUNDARY_ROLES = frozenset(
    {
        "AXFooter",
        "AXHeading",
        "AXList",
        "AXMenuItem",
        "AXSlider",
        "AXTable",
        "AXWebArea",
    }
)


def extract_article_body(
    paragraphs: Sequence[Mapping[str, Any]],
) -> str:
    """Choose one unambiguous rendered prose column from DOM ``<p>`` rows."""

    runs: list[tuple[tuple[int, int], list[str], bool]] = []
    current_layout: tuple[int, int] | None = None
    current_all_reuters_paragraphs = False
    current_top: int | None = None
    current_texts: list[str] = []

    def finish_run() -> None:
        nonlocal current_all_reuters_paragraphs, current_layout, current_top
        nonlocal current_texts
        if current_layout is not None and current_texts:
            runs.append(
                (
                    current_layout,
                    current_texts,
                    current_all_reuters_paragraphs,
                )
            )
        current_layout = None
        current_all_reuters_paragraphs = False
        current_top = None
        current_texts = []

    for paragraph in paragraphs:
        rendered = _rendered_paragraph(paragraph)
        if rendered is None:
            finish_run()
            continue

        text, layout, top, is_reuters_paragraph = rendered
        if current_layout != layout or (
            current_top is not None and top <= current_top
        ):
            finish_run()
            current_layout = layout
            current_all_reuters_paragraphs = is_reuters_paragraph
        else:
            current_all_reuters_paragraphs = (
                current_all_reuters_paragraphs and is_reuters_paragraph
            )
        current_texts.append(text)
        current_top = top
    finish_run()

    candidates = [
        run
        for run in runs
        if len(run[1]) >= _MIN_BODY_PARAGRAPHS
        or _is_single_reuters_body_run(run)
    ]
    if not candidates:
        raise ReaderError("DOM did not contain a rendered paragraph group")

    best_score = max(_paragraph_run_score(run) for run in candidates)
    winners = [
        run for run in candidates if _paragraph_run_score(run) == best_score
    ]
    if len(winners) != 1:
        raise ReaderError("DOM article paragraph group is ambiguous")

    winner_layout, body_paragraphs, _winner_all_reuters_paragraphs = winners[0]
    if any(
        layout == winner_layout and texts is not body_paragraphs
        for layout, texts, _all_reuters_paragraphs in runs
    ):
        raise ReaderError("DOM article paragraph column is interrupted")

    return "\n\n".join(body_paragraphs)


def extract_ax_article_body(
    snapshot: Mapping[str, Any],
    expected_title: str,
) -> str:
    """Reconstruct one dominant prose run from the H1's AX siblings."""

    elements = snapshot.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(element, Mapping) for element in elements
    ):
        raise AxExtractionError("AX snapshot elements are invalid")

    degraded_reason = snapshot.get("degraded_reason")
    if degraded_reason not in (None, ""):
        raise AxExtractionError("AX snapshot reports degraded coverage")

    returned_count = _ax_snapshot_count(snapshot, "returned_element_count")
    total_count = _ax_snapshot_count(snapshot, "total_element_count")
    if returned_count != total_count or returned_count != len(elements):
        raise AxExtractionError("AX snapshot element coverage is incomplete")

    headings = [
        element
        for element in elements
        if element.get("role") == "AXHeading"
        and str(element.get("value", "")) == "1"
        and _same_title(_element_text(element), expected_title)
    ]
    if len(headings) != 1:
        raise AxExtractionError("AX snapshot does not contain one exact article H1")

    heading = headings[0]
    heading_index = _ax_element_index(heading)
    parent_index = heading.get("parent_index")
    if not isinstance(parent_index, int) or isinstance(parent_index, bool):
        raise AxExtractionError("AX article H1 has no ordered parent")

    siblings: list[Mapping[str, Any]] = []
    sibling_indexes: set[int] = set()
    for element in elements:
        if element.get("parent_index") != parent_index:
            continue
        element_index = _ax_element_index(element)
        if element_index in sibling_indexes:
            raise AxExtractionError("AX article siblings have duplicate indexes")
        sibling_indexes.add(element_index)
        if element_index > heading_index:
            siblings.append(element)
    siblings.sort(key=_ax_element_index)
    if not siblings:
        raise AxExtractionError("AX article H1 has no following siblings")

    runs = _ax_prose_runs(siblings)
    substantial = [
        run for run in runs if len(run[0]) >= _MIN_BODY_PARAGRAPHS
    ]
    if not substantial:
        raise AxExtractionError("AX did not contain a multi-paragraph prose run")
    if any(run[2] for run in substantial):
        raise AxExtractionError(
            "AX cannot distinguish a standalone link from link-leading prose"
        )

    best_score = max(_ax_run_score(run) for run in substantial)
    winners = [
        run for run in substantial if _ax_run_score(run) == best_score
    ]
    if len(winners) != 1:
        raise AxExtractionError("AX article prose run is ambiguous")

    body_paragraphs, is_incomplete, _link_leading = winners[0]
    if is_incomplete:
        raise AxExtractionError("AX article prose run ends with an incomplete fragment")

    other_characters = sum(
        _ax_run_score(run)[0]
        for run in substantial
        if run is not winners[0]
    )
    if best_score[0] <= other_characters:
        raise AxExtractionError("AX article prose run is not dominant")

    return "\n\n".join(body_paragraphs)


def _same_title(actual: str, expected: str) -> bool:
    """Compare titles exactly apart from whitespace and typographic quote variants."""

    return _normalized_title(actual) == _normalized_title(expected)


def _normalized_title(value: str) -> str:
    return " ".join(value.translate(_TITLE_QUOTE_TRANSLATION).split())


def _ax_prose_runs(
    siblings: Sequence[Mapping[str, Any]],
) -> list[tuple[list[str], bool, bool]]:
    runs: list[tuple[list[str], bool, bool]] = []
    paragraphs: list[str] = []
    fragments: list[str] = []
    link_leading = False

    def finish_run() -> None:
        nonlocal paragraphs, fragments, link_leading
        if paragraphs:
            runs.append((paragraphs, bool(fragments), link_leading))
        paragraphs = []
        fragments = []
        link_leading = False

    for position, element in enumerate(siblings):
        role = element.get("role")
        if role == "AXStaticText":
            text = _element_text(element)
            if not text:
                continue
            if _looks_like_ax_credit(text):
                if fragments:
                    fragments = []
                elif paragraphs:
                    paragraphs.pop()
                continue

            fragments.append(text)
            paragraph = _join_ax_fragments(fragments)
            if _ends_ax_sentence(paragraph):
                paragraphs.append(paragraph)
                fragments = []
            continue

        if role == "AXLink":
            text = _element_text(element)
            if fragments and text:
                fragments.append(text)
                paragraph = _join_ax_fragments(fragments)
                if _ends_ax_sentence(paragraph):
                    paragraphs.append(paragraph)
                    fragments = []
            else:
                if text and _ax_link_may_start_prose(siblings, position):
                    finish_run()
                    fragments = [text]
                    link_leading = True
                else:
                    finish_run()
            continue

        # Pages can expose inline controls and media cards between prose
        # fragments. Their labels are interface text, not article text.
        if role in {"AXButton", "AXImage"}:
            continue

        if role in _AX_RUN_BOUNDARY_ROLES or role is not None:
            finish_run()

    finish_run()
    return runs


def _ax_link_may_start_prose(
    siblings: Sequence[Mapping[str, Any]],
    position: int,
) -> bool:
    for following in siblings[position + 1 :]:
        role = following.get("role")
        if role in {"AXButton", "AXImage"}:
            continue
        return role == "AXStaticText" and bool(_element_text(following))
    return False


def _ax_snapshot_count(snapshot: Mapping[str, Any], key: str) -> int:
    value = snapshot.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AxExtractionError(f"AX snapshot is missing {key}")
    return value


def _ax_element_index(element: Mapping[str, Any]) -> int:
    value = element.get("element_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AxExtractionError("AX article sibling has no element index")
    return value


def _ax_run_score(run: tuple[list[str], bool, bool]) -> tuple[int, int]:
    return sum(len(paragraph) for paragraph in run[0]), len(run[0])


def _looks_like_ax_credit(text: str) -> bool:
    if len(text) > _AX_CREDIT_MAX_CHARACTERS or text.count("|") != 1:
        return False
    left, right = (part.strip() for part in text.split("|", maxsplit=1))
    return bool(left and right)


def _ends_ax_sentence(text: str) -> bool:
    return text.rstrip("\"'”’)]}").endswith((".", "?", "!"))


def _join_ax_fragments(fragments: Sequence[str]) -> str:
    result = ""
    for fragment in fragments:
        normalized = " ".join(fragment.split())
        if not normalized:
            continue
        if not result or normalized[0] in ".,;:!?)]}’'":
            result += normalized
        else:
            result += f" {normalized}"
    return result


def _rendered_paragraph(
    paragraph: Mapping[str, Any],
) -> tuple[str, tuple[int, int], int, bool] | None:
    tag_name = paragraph.get("tagName")
    inner_text = paragraph.get("innerText")
    hidden = paragraph.get("hidden")
    left = paragraph.get("offsetLeft")
    top = paragraph.get("offsetTop")
    width = paragraph.get("offsetWidth")
    height = paragraph.get("offsetHeight")

    is_reuters_paragraph = _is_reuters_paragraph_div(paragraph, tag_name)
    if not _is_dom_paragraph_element(paragraph, tag_name):
        raise ReaderError("query_dom returned a non-paragraph DOM element")
    if not isinstance(inner_text, str) or not isinstance(hidden, bool):
        raise ReaderError("query_dom returned invalid paragraph text or visibility")
    if not all(
        _is_finite_number(value) for value in (left, top, width, height)
    ):
        raise ReaderError("query_dom returned invalid paragraph dimensions")

    text = " ".join(inner_text.split())
    if hidden or width <= 0 or height <= 0 or not text:
        return None
    return text, (round(left), round(width)), round(top), is_reuters_paragraph


def _is_dom_paragraph_element(
    paragraph: Mapping[str, Any],
    tag_name: object,
) -> bool:
    if not isinstance(tag_name, str):
        return False
    normalized_tag = tag_name.casefold()
    if normalized_tag == "p":
        return True
    return _is_reuters_paragraph_div(paragraph, tag_name)


def _is_reuters_paragraph_div(
    paragraph: Mapping[str, Any],
    tag_name: object,
) -> bool:
    if not isinstance(tag_name, str) or tag_name.casefold() != "div":
        return False
    test_id = paragraph.get("data-testid")
    return (
        isinstance(test_id, str)
        and test_id.startswith(_REUTERS_PARAGRAPH_TEST_ID_PREFIX)
    )


def _is_single_reuters_body_run(
    run: tuple[tuple[int, int], list[str], bool],
) -> bool:
    _layout, texts, all_reuters_paragraphs = run
    return (
        all_reuters_paragraphs
        and len(texts) == 1
        and len(texts[0]) >= _MIN_SINGLE_REUTERS_BODY_CHARACTERS
    )


def _paragraph_run_score(
    run: tuple[tuple[int, int], list[str], bool],
) -> tuple[int, int]:
    return sum(len(text) for text in run[1]), len(run[1])


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
    )


def _element_text(element: Mapping[str, Any]) -> str:
    for key in ("label", "value"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""
