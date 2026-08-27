"""Tests for the hostile legacy credit-union target app."""

from __future__ import annotations

import re

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

import target_app.app as target
from target_app.app import create_app

USER = "tclark"
PASSWORD = "spring2026-demo"
EXPIRED = "Your session has expired. Please sign in again."

HIDDEN_RE = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


@pytest.fixture()
def app() -> Flask:
    return create_app()


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def login(client: FlaskClient) -> None:
    resp = client.post("/login", data={"txtUser": USER, "txtPass": PASSWORD})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/app"


def html_of(resp: TestResponse) -> str:
    return resp.get_data(as_text=True)


def set_fault(client: FlaskClient, fault: str) -> None:
    resp = client.post("/debug/fault", data={"fault": fault})
    assert resp.status_code == 200


def current_fault(client: FlaskClient) -> str:
    return client.get("/debug/fault").get_data(as_text=True)


def assert_hostile(html: str) -> None:
    assert 'id="' not in html
    assert "data-testid" not in html
    assert "<label" not in html


# --------------------------------------------------------------------------
# Login and sessions
# --------------------------------------------------------------------------

class TestLogin:
    def test_login_page_renders(self, client: FlaskClient) -> None:
        resp = client.get("/login")
        assert resp.status_code == 200
        html = html_of(resp)
        assert "Teller Sign-In" in html
        assert 'name="txtUser"' in html
        assert 'name="txtPass"' in html
        assert '<input type="submit" value="Sign In">' in html
        assert_hostile(html)

    def test_login_failure_exact_string(self, client: FlaskClient) -> None:
        resp = client.post("/login", data={"txtUser": USER, "txtPass": "wrong"})
        assert resp.status_code == 200
        html = html_of(resp)
        assert "Invalid credentials." in html
        assert "Teller Sign-In" in html

    def test_login_success_redirects_to_app(self, client: FlaskClient) -> None:
        login(client)
        resp = client.get("/app")
        assert resp.status_code == 200

    def test_session_expired_message_without_session(self, client: FlaskClient) -> None:
        resp = client.get("/app/members")
        assert resp.status_code == 200
        html = html_of(resp)
        assert EXPIRED in html
        assert "Teller Sign-In" in html


# --------------------------------------------------------------------------
# Chrome and home
# --------------------------------------------------------------------------

class TestChrome:
    def test_chrome_banner_nav_and_iframe(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app"))
        assert "Cornerstone Federal Credit Union — Teller System" in html
        assert '<a href="/app/members" target="workarea">Member Services</a>' in html
        assert '<a href="/app/reports" target="workarea">Reports</a>' in html
        assert '<a href="/app/admin" target="workarea">Administration</a>' in html
        assert '<iframe name="workarea" src="/app/home" width="100%" height="600">' in html
        assert_hostile(html)

    def test_home_page(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/home"))
        assert "Main Menu" in html
        assert "Welcome, T. Clark (Teller)" in html


# --------------------------------------------------------------------------
# Member search and profile
# --------------------------------------------------------------------------

class TestMemberSearch:
    def test_search_form(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/members"))
        assert "Member Search" in html
        assert "Member Number:" in html
        assert '<input type="text" name="txtQ">' in html
        assert '<input type="submit" value="Search">' in html
        assert_hostile(html)

    @pytest.mark.parametrize("query", ["", "   ", "abc", "12a45"])
    def test_non_numeric_validation(self, client: FlaskClient, query: str) -> None:
        login(client)
        html = html_of(client.post("/app/members", data={"txtQ": query}))
        assert "Member number must be numeric." in html
        assert "Member Search" in html

    def test_not_found(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.post("/app/members", data={"txtQ": "99999"}))
        assert "Member Search" in html
        assert "No matching member records were found." in html

    def test_known_member_redirects(self, client: FlaskClient) -> None:
        login(client)
        resp = client.post("/app/members", data={"txtQ": "12345"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/app/members/12345"


class TestMemberProfile:
    def test_profile_contents(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/members/12345"))
        assert "Member Profile" in html
        assert "Margaret Ellison" in html
        assert "Regular Savings" in html
        assert "$4,982.17" in html
        assert "000-12-3456" in html
        assert "Acct No" in html and "Current Balance" in html
        assert "Open Sub-Account" in html
        assert_hostile(html)

    def test_profile_unknown_member(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/members/99999"))
        assert "No matching member records were found." in html

    def test_checking_only_member_has_no_savings_row(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/members/20001"))
        assert "Priya Raman" in html
        assert "Checking" in html
        assert "Regular Savings" not in html


# --------------------------------------------------------------------------
# Sub-account flow
# --------------------------------------------------------------------------

SUBACCT_URL = "/app/members/12345/subaccount/new"
VALID_ENTRY = {"selType": "Holiday Club", "txtDep": "50", "selFund": "12345-S1"}


class TestSubAccount:
    def test_entry_form_fields(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get(SUBACCT_URL))
        assert "Open Sub-Account" in html
        assert 'name="selType"' in html
        assert "Holiday Club" in html and "Money Market" in html
        assert 'name="txtDep"' in html
        assert 'name="selFund"' in html
        assert "12345-S1" in html and "12345-D1" in html
        assert '<input type="submit" value="Continue">' in html

    def test_deposit_not_a_number(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.post(SUBACCT_URL, data={**VALID_ENTRY, "txtDep": "abc"}))
        assert "Initial deposit must be a dollar amount." in html

    def test_deposit_below_minimum(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.post(SUBACCT_URL, data={**VALID_ENTRY, "txtDep": "10"}))
        assert "Minimum initial deposit is $25.00." in html

    def test_full_flow_opens_c1(self, client: FlaskClient) -> None:
        login(client)
        review = html_of(client.post(SUBACCT_URL, data=VALID_ENTRY))
        assert "Review Sub-Account Request" in review
        assert '<input type="submit" name="btnConfirm" value="Confirm Open">' in review
        assert '<input type="submit" name="btnBack" value="Go Back">' in review

        # Submit the review form the way a browser would: hidden fields + button.
        confirm = dict(HIDDEN_RE.findall(review))
        assert confirm  # hidden fields must carry the choices
        confirm["btnConfirm"] = "Confirm Open"
        done = html_of(client.post(SUBACCT_URL, data=confirm))
        assert "Sub-Account Opened" in done
        assert "Sub-account opened successfully. New account number: 12345-C1." in done

    def test_repeat_open_increments_suffix(self, client: FlaskClient) -> None:
        login(client)
        for expected in ("12345-C1", "12345-C2"):
            review = html_of(client.post(SUBACCT_URL, data=VALID_ENTRY))
            confirm = dict(HIDDEN_RE.findall(review))
            confirm["btnConfirm"] = "Confirm Open"
            done = html_of(client.post(SUBACCT_URL, data=confirm))
            assert f"New account number: {expected}." in done

    def test_go_back_returns_to_form(self, client: FlaskClient) -> None:
        login(client)
        review = html_of(client.post(SUBACCT_URL, data=VALID_ENTRY))
        back = dict(HIDDEN_RE.findall(review))
        back["btnBack"] = "Go Back"
        html = html_of(client.post(SUBACCT_URL, data=back))
        assert "Open Sub-Account" in html
        assert 'name="txtDep"' in html


# --------------------------------------------------------------------------
# Reports, admin, purge, healthz
# --------------------------------------------------------------------------

class TestOtherPages:
    def test_reports(self, client: FlaskClient) -> None:
        login(client)
        html = html_of(client.get("/app/reports"))
        assert "Reports" in html
        assert "Select a report from the list." in html

    def test_admin_not_authorized(self, client: FlaskClient) -> None:
        login(client)
        resp = client.get("/app/admin")
        assert resp.status_code == 200
        html = html_of(resp)
        assert "Administration" in html
        assert "You are not authorized to access this function." in html
        assert '<a href="/admin/purge">Purge Member Records</a>' in html

    def test_purge_page(self, client: FlaskClient) -> None:
        html = html_of(client.get("/admin/purge"))
        assert "Purge Member Records" in html
        assert "WARNING: This will permanently delete records." in html
        assert '<input type="submit" value="Purge All Records">' in html

    def test_purge_post_refused(self, client: FlaskClient) -> None:
        html = html_of(client.post("/admin/purge"))
        assert "Operation not permitted." in html

    def test_healthz_no_auth(self, client: FlaskClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_data(as_text=True) == "ok"


# --------------------------------------------------------------------------
# Fault injection (each fault is one-shot: set -> triggers once -> cleared)
# --------------------------------------------------------------------------

class TestFaults:
    def test_default_is_none(self, client: FlaskClient) -> None:
        assert current_fault(client) == "none"

    def test_unknown_fault_rejected(self, client: FlaskClient) -> None:
        resp = client.post("/debug/fault", data={"fault": "chaos"})
        assert resp.status_code == 400
        assert current_fault(client) == "none"

    def test_slow_one_shot(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        naps: list[float] = []
        monkeypatch.setattr(target.time, "sleep", naps.append)
        login(client)
        set_fault(client, "slow")
        assert current_fault(client) == "slow"

        resp = client.get("/app/home")
        assert resp.status_code == 200
        assert naps == [target._SLOW_SECONDS]
        assert current_fault(client) == "none"

        client.get("/app/home")
        assert naps == [target._SLOW_SECONDS]  # no second sleep

    def test_error500_one_shot(self, client: FlaskClient) -> None:
        login(client)
        set_fault(client, "error500")

        resp = client.get("/app/home")
        assert resp.status_code == 500
        html = html_of(resp)
        assert "System Error" in html
        assert "An unexpected error occurred. Reference #55-1017." in html
        assert current_fault(client) == "none"

        resp2 = client.get("/app/home")
        assert resp2.status_code == 200
        assert "Main Menu" in html_of(resp2)

    def test_timeout_next_one_shot(self, client: FlaskClient) -> None:
        login(client)
        set_fault(client, "timeout_next")

        resp = client.get("/app/home")
        assert resp.status_code == 200
        assert EXPIRED in html_of(resp)
        assert current_fault(client) == "none"

        login(client)  # session was cleared; a fresh login works again
        assert "Main Menu" in html_of(client.get("/app/home"))

    def test_notice_next_one_shot(self, client: FlaskClient) -> None:
        login(client)
        set_fault(client, "notice_next")

        html = html_of(client.get("/app/members"))
        assert "Daily Notice" in html
        assert "Reminder: Nightly batch processing begins at 9:00 PM CT." in html
        assert '<input type="hidden" name="hidNext" value="/app/members">' in html
        assert '<input type="submit" value="Continue">' in html
        assert current_fault(client) == "none"

        ack = client.post("/app/notice/ack", data={"hidNext": "/app/members"})
        assert ack.status_code == 302
        assert ack.headers["Location"] == "/app/members"
        assert "Member Search" in html_of(client.get("/app/members"))

    def test_notice_ack_rejects_external_target(self, client: FlaskClient) -> None:
        login(client)
        ack = client.post("/app/notice/ack", data={"hidNext": "https://evil.example/"})
        assert ack.status_code == 302
        assert ack.headers["Location"] == "/app/home"


# --------------------------------------------------------------------------
# Hostility sweep: no ids, no data-testids, no labels, no scripts — anywhere
# --------------------------------------------------------------------------

def test_every_page_is_hostile(app: Flask, client: FlaskClient) -> None:
    login(client)
    htmls: dict[str, str] = {}
    for path in (
        "/login", "/app", "/app/home", "/app/members", "/app/members/12345",
        "/app/members/12345/subaccount/new", "/app/members/99999",
        "/app/reports", "/app/admin", "/admin/purge",
    ):
        htmls[path] = html_of(client.get(path))
    htmls["review"] = html_of(client.post(SUBACCT_URL, data=VALID_ENTRY))
    htmls["opened"] = html_of(client.post(
        SUBACCT_URL,
        data={"btnConfirm": "Confirm Open", "hidType": "Holiday Club",
              "hidDep": "50.00", "hidFund": "12345-S1"},
    ))
    set_fault(client, "notice_next")
    htmls["notice"] = html_of(client.get("/app/home"))
    set_fault(client, "error500")
    htmls["error500"] = html_of(client.get("/app/home"))
    htmls["expired"] = html_of(app.test_client().get("/app/members"))

    for name, html in htmls.items():
        for needle in ('id="', "data-testid", "<label", "<script"):
            assert needle not in html, f"{needle!r} found in {name}"
