"""Unit tests for the distillation helpers (rote/distill.py).

All inputs are constructed directly; no browser and no LLM are involved.
"""

from __future__ import annotations

from rote.discover import TraceStep
from rote.distill import (
    _checkpoint_after,
    _heading,
    _make_templatizer,
    _pick_row_label,
    _target_from_element,
)
from rote.surface import MAIN_FRAME, Element


def make_element(**overrides) -> Element:
    defaults = dict(
        index=0,
        frame=MAIN_FRAME,
        tag="input",
        role="textbox",
        name="",
        input_name="",
        label="",
        value_hint="",
        href="",
        type="text",
        disabled=False,
        dom_path="body > form:nth-of-type(1) > input:nth-of-type(2)",
        options=(),
    )
    defaults.update(overrides)
    return Element(**defaults)


def make_trace_step(**overrides) -> TraceStep:
    defaults = dict(
        tool="click",
        why="open the member detail",
        element=make_element(tag="a", role="link", name="Detail"),
        value=None,
        url=None,
        url_before="http://127.0.0.1:7710/search",
        url_after="http://127.0.0.1:7710/member/12345",
        frame_text_after={MAIN_FRAME: "Member Detail\nBalance: $4,982.17"},
        risk="safe",
    )
    defaults.update(overrides)
    return TraceStep(**defaults)


identity = _make_templatizer({})


# ---------------------------------------------------------------------------
# _make_templatizer
# ---------------------------------------------------------------------------


def test_templatizer_replaces_longest_literal_first():
    templatize = _make_templatizer({"share": "345", "member_id": "12345"})
    # "12345" must win over its substring "345"
    assert templatize("Account 12345 opened") == "Account {{param:member_id}} opened"
    assert templatize("Account 12345 and share 345") == (
        "Account {{param:member_id}} and share {{param:share}}"
    )


def test_templatizer_produces_member_id_placeholder():
    templatize = _make_templatizer({"member_id": "12345"})
    assert templatize("12345") == "{{param:member_id}}"


def test_templatizer_handles_none_and_plain_text():
    templatize = _make_templatizer({"member_id": "12345"})
    assert templatize(None) == ""
    assert templatize("no literals here") == "no literals here"


# ---------------------------------------------------------------------------
# _target_from_element
# ---------------------------------------------------------------------------


def test_target_ranking_role_label_attr_dom_path():
    el = make_element(
        tag="input",
        role="button",
        name="Search",
        label="Search:",
        input_name="btnSearch",
        type="submit",
    )
    ref = _target_from_element(el)

    strategies = [loc.strategy for loc in ref.locators]
    assert strategies == ["role", "label_near", "attr", "dom_path"]

    confidences = [loc.confidence for loc in ref.locators]
    assert confidences == sorted(confidences, reverse=True)
    assert len(set(confidences)) == len(confidences)  # strictly descending

    assert ref.locators[0].value == {"role": "button", "name": "Search"}
    assert ref.locators[2].value == {"selector": "input[name='btnSearch']"}
    assert ref.frame is None  # main-frame elements carry no frame name


def test_target_for_link_adds_text_strategy_and_dom_path_stays_last():
    el = make_element(tag="a", role="link", name="Member Detail", href="/member", frame="workarea")
    ref = _target_from_element(el)

    strategies = [loc.strategy for loc in ref.locators]
    assert strategies == ["role", "text", "dom_path"]
    text_loc = ref.locators[1]
    assert text_loc.value == {"text": "Member Detail"}
    assert ref.locators[-1].strategy == "dom_path"
    assert ref.frame == "workarea"


def test_dom_path_is_always_present_and_last():
    el = make_element()  # a bare textbox: no role/label/attr facets at all
    ref = _target_from_element(el)
    assert [loc.strategy for loc in ref.locators] == ["dom_path"]


# ---------------------------------------------------------------------------
# _pick_row_label
# ---------------------------------------------------------------------------


def test_pick_row_label_prefers_digit_free_cell():
    cells = ["12345-S1", "Regular Savings", "$4,982.17"]
    assert _pick_row_label(cells, "$4,982.17") == "Regular Savings"


def test_pick_row_label_falls_back_to_first_non_value_cell():
    cells = ["", "12345-S1", "$4,982.17"]
    assert _pick_row_label(cells, "$4,982.17") == "12345-S1"


def test_pick_row_label_never_returns_the_value_itself():
    assert _pick_row_label(["$4,982.17"], "$4,982.17") is None


# ---------------------------------------------------------------------------
# _heading
# ---------------------------------------------------------------------------


def test_heading_returns_first_non_empty_line():
    assert _heading("\n\n   Member Detail  \nBalance: $5.00") == "Member Detail"
    assert _heading("") == ""
    assert _heading("\n  \n") == ""


# ---------------------------------------------------------------------------
# _checkpoint_after
# ---------------------------------------------------------------------------


def test_checkpoint_after_click_asserts_new_headings_text():
    ts = make_trace_step(frame_text_after={MAIN_FRAME: "Member Detail\nBalance: $4,982.17"})
    cp = _checkpoint_after(ts, prev_text={MAIN_FRAME: "Member Search"}, templatize=identity)

    assert cp is not None
    assert cp.condition.kind == "text_visible"
    assert cp.condition.value == "Member Detail"
    assert cp.condition.frame is None  # main frame -> no frame name
    assert ts.why in cp.description


def test_checkpoint_after_prefers_the_changed_subframe():
    ts = make_trace_step(
        frame_text_after={MAIN_FRAME: "chrome", "workarea": "Member Detail\nrows"},
    )
    cp = _checkpoint_after(
        ts, prev_text={MAIN_FRAME: "chrome", "workarea": "Member Search"}, templatize=identity
    )
    assert cp is not None
    assert cp.condition.value == "Member Detail"
    assert cp.condition.frame == "workarea"


def test_checkpoint_value_is_templatized():
    templatize = _make_templatizer({"member_id": "12345"})
    ts = make_trace_step(frame_text_after={MAIN_FRAME: "Member 12345\ndetails"})
    cp = _checkpoint_after(ts, prev_text={}, templatize=templatize)
    assert cp is not None
    assert cp.condition.value == "Member {{param:member_id}}"


def test_checkpoint_after_fill_is_none():
    ts = make_trace_step(tool="fill", value="12345")
    assert _checkpoint_after(ts, prev_text={}, templatize=identity) is None


def test_checkpoint_after_click_with_blank_screen_is_none():
    ts = make_trace_step(frame_text_after={MAIN_FRAME: "\n  \n"})
    assert _checkpoint_after(ts, prev_text={MAIN_FRAME: "something"}, templatize=identity) is None
