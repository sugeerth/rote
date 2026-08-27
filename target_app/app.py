"""Cornerstone Federal Credit Union — Teller System (demo proxy target).

A deliberately hostile, 2003-era server-rendered Flask app. All layout is
nested tables with <font> tags, bgcolor attributes, and inline styles. There
are no element ids, no data-testid attributes, no <label> tags, and no
JavaScript — automation must navigate by visible text and cryptic field names.

Business pages live under /app/* and render inside an <iframe name="workarea">
hosted by the /app chrome page. A one-shot fault injector (/debug/fault) lets
error-replay demos trigger slowness, 500s, session timeouts, and interstitials.

Run standalone:  python3 -m target_app.app
Port comes from ROTE_TARGET_PORT (default 7710), bound to 127.0.0.1.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from flask import Flask, Response, make_response, redirect, request, session
from markupsafe import escape

from target_app.data import CREDENTIALS, MEMBERS, ROLE, Member

_SLOW_SECONDS: float = 8.0
_FAULTS = ("none", "slow", "error500", "timeout_next", "notice_next")
_DEPOSIT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")

SESSION_EXPIRED_MSG = "Your session has expired. Please sign in again."


# --------------------------------------------------------------------------
# HTML builders — pure string soup, exactly like it's 2003.
# --------------------------------------------------------------------------

def _page(title: str, body: str) -> str:
    return (
        "<html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        f"<title>{title}</title></head>"
        '<body bgcolor="#C0C0C0" text="#000000" link="#000080" vlink="#000080" '
        'style="margin: 8px; font-family: Verdana, Arial, sans-serif;">'
        f"{body}</body></html>"
    )


def _heading(text: str) -> str:
    return (
        '<table width="100%" border="0" cellpadding="3" cellspacing="0">'
        '<tr><td bgcolor="#000080">'
        f'<font color="#FFFFFF" face="Verdana" size="3"><b>{text}</b></font>'
        "</td></tr></table>"
    )


def _red(text: str) -> str:
    return f'<font color="red"><b>{text}</b></font>'


def _login_html(message: str | None = None) -> str:
    rows = ""
    if message:
        rows += f'<tr><td colspan="2">{_red(message)}</td></tr>'
    rows += (
        '<tr><td align="right"><font size="2">User ID:</font></td>'
        '<td><input type="text" name="txtUser" size="16"></td></tr>'
        '<tr><td align="right"><font size="2">Password:</font></td>'
        '<td><input type="password" name="txtPass" size="16"></td></tr>'
        '<tr><td>&nbsp;</td><td><input type="submit" value="Sign In"></td></tr>'
    )
    body = (
        _heading("Teller Sign-In")
        + '<form method="post" action="/login">'
        + '<table border="1" cellpadding="4" cellspacing="0" bgcolor="#FFFFFF">'
        + '<tr><td colspan="2" bgcolor="#D4D0C8">'
        + '<font size="2"><b>Please enter your teller credentials.</b></font></td></tr>'
        + rows
        + "</table></form>"
    )
    return _page("Teller Sign-In", body)


def _chrome_html() -> str:
    nav_links = "".join(
        '<tr><td bgcolor="#D4D0C8" style="border-bottom: 1px solid #808080;">'
        f'<font size="2"><a href="{href}" target="workarea">{label}</a></font></td></tr>'
        for label, href in (
            ("Member Services", "/app/members"),
            ("Reports", "/app/reports"),
            ("Administration", "/app/admin"),
        )
    )
    body = (
        '<table width="100%" border="0" cellpadding="6" cellspacing="0">'
        '<tr><td bgcolor="#003366">'
        '<font color="#FFFFFF" face="Verdana" size="4"><b>'
        "Cornerstone Federal Credit Union — Teller System"
        "</b></font></td></tr></table>"
        '<table width="100%" border="0" cellpadding="4" cellspacing="0"><tr>'
        '<td width="170" valign="top" bgcolor="#D4D0C8">'
        '<table width="100%" border="0" cellpadding="4" cellspacing="0">'
        + nav_links
        + '<tr><td bgcolor="#D4D0C8"><font size="2"><a href="/logout">Sign Out</a></font></td></tr>'
        "</table></td>"
        '<td valign="top" bgcolor="#FFFFFF">'
        '<iframe name="workarea" src="/app/home" width="100%" height="600"></iframe>'
        "</td></tr></table>"
    )
    return _page("Cornerstone Federal Credit Union — Teller System", body)


def _home_html() -> str:
    body = (
        _heading("Main Menu")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + '<tr><td><font size="2">Welcome, T. Clark (Teller)</font></td></tr>'
        + '<tr><td><font size="2">Select a function from the menu at left.</font></td></tr>'
        + "</table>"
    )
    return _page("Main Menu", body)


def _search_html(error: str | None = None) -> str:
    err = f'<tr><td colspan="3">{_red(error)}</td></tr>' if error else ""
    body = (
        _heading("Member Search")
        + '<form method="post" action="/app/members">'
        + '<table border="1" cellpadding="4" cellspacing="0" bgcolor="#FFFFFF">'
        + err
        + '<tr><td><font size="2">Member Number:</font></td>'
        + '<td><input type="text" name="txtQ"></td>'
        + '<td><input type="submit" value="Search"></td></tr>'
        + "</table></form>"
    )
    return _page("Member Search", body)


def _not_found_html() -> str:
    body = (
        _heading("Member Search")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + '<tr><td><font size="2">No matching member records were found.</font></td></tr>'
        + '<tr><td><font size="2"><a href="/app/members">Return to Member Search</a></font></td></tr>'
        + "</table>"
    )
    return _page("Member Search", body)


def _profile_html(num: str, member: Member) -> str:
    info_rows = "".join(
        f'<tr><td bgcolor="#D4D0C8"><font size="2"><b>{label}</b></font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{value}</font></td></tr>'
        for label, value in (
            ("Member Number:", num),
            ("Name:", member["name"]),
            ("Status:", member["status"]),
            ("Member Since:", member["joined"]),
            ("SSN on File:", member["ssn"]),
        )
    )
    acct_header = "".join(
        f'<td bgcolor="#000080"><font color="#FFFFFF" size="2"><b>{h}</b></font></td>'
        for h in ("Acct No", "Type", "Status", "Current Balance")
    )
    acct_rows = "".join(
        f'<tr><td bgcolor="#FFFFFF"><font size="2">{a}</font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{t}</font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{s}</font></td>'
        f'<td bgcolor="#FFFFFF" align="right"><font size="2">{b}</font></td></tr>'
        for a, t, s, b in member["accounts"]
    )
    body = (
        _heading("Member Profile")
        + f'<table border="1" cellpadding="4" cellspacing="0">{info_rows}</table><br>'
        + '<table border="1" cellpadding="4" cellspacing="0">'
        + f"<tr>{acct_header}</tr>{acct_rows}</table><br>"
        + f'<font size="2"><a href="/app/members/{num}/subaccount/new">Open Sub-Account</a></font>'
    )
    return _page("Member Profile", body)


def _subaccount_form_html(num: str, member: Member, error: str | None = None) -> str:
    err = f'<tr><td colspan="2">{_red(error)}</td></tr>' if error else ""
    fund_options = "".join(f"<option>{acct[0]}</option>" for acct in member["accounts"])
    body = (
        _heading("Open Sub-Account")
        + f'<form method="post" action="/app/members/{num}/subaccount/new">'
        + '<table border="1" cellpadding="4" cellspacing="0" bgcolor="#FFFFFF">'
        + err
        + '<tr><td><font size="2">Account Type:</font></td>'
        + '<td><select name="selType"><option>Holiday Club</option>'
        + "<option>Money Market</option></select></td></tr>"
        + '<tr><td><font size="2">Initial Deposit:</font></td>'
        + '<td><input type="text" name="txtDep"></td></tr>'
        + '<tr><td><font size="2">Fund From:</font></td>'
        + f'<td><select name="selFund">{fund_options}</select></td></tr>'
        + '<tr><td>&nbsp;</td><td><input type="submit" value="Continue"></td></tr>'
        + "</table></form>"
    )
    return _page("Open Sub-Account", body)


def _review_html(num: str, sel_type: str, deposit: str, fund: str) -> str:
    t, d, f = escape(sel_type), escape(deposit), escape(fund)
    rows = "".join(
        f'<tr><td bgcolor="#D4D0C8"><font size="2"><b>{label}</b></font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{value}</font></td></tr>'
        for label, value in (
            ("Member Number:", num),
            ("Account Type:", t),
            ("Initial Deposit:", f"${d}"),
            ("Fund From:", f),
        )
    )
    body = (
        _heading("Review Sub-Account Request")
        + f'<table border="1" cellpadding="4" cellspacing="0">{rows}</table><br>'
        + f'<form method="post" action="/app/members/{num}/subaccount/new">'
        + f'<input type="hidden" name="hidType" value="{t}">'
        + f'<input type="hidden" name="hidDep" value="{d}">'
        + f'<input type="hidden" name="hidFund" value="{f}">'
        + '<input type="submit" name="btnConfirm" value="Confirm Open"> '
        + '<input type="submit" name="btnBack" value="Go Back">'
        + "</form>"
    )
    return _page("Review Sub-Account Request", body)


def _opened_html(num: str, acct: str) -> str:
    body = (
        _heading("Sub-Account Opened")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + f'<tr><td><font size="2">Sub-account opened successfully. New account number: {acct}.</font></td></tr>'
        + f'<tr><td><font size="2"><a href="/app/members/{num}">Return to Member Profile</a></font></td></tr>'
        + "</table>"
    )
    return _page("Sub-Account Opened", body)


def _reports_html() -> str:
    header = "".join(
        f'<td bgcolor="#000080"><font color="#FFFFFF" size="2"><b>{h}</b></font></td>'
        for h in ("Report Name", "Last Run", "Status")
    )
    rows = "".join(
        f'<tr><td bgcolor="#FFFFFF"><font size="2">{n}</font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{d}</font></td>'
        f'<td bgcolor="#FFFFFF"><font size="2">{s}</font></td></tr>'
        for n, d, s in (
            ("Daily Transaction Journal", "08/26/2026", "Complete"),
            ("Dormant Account Listing", "07/31/2026", "Complete"),
            ("Quarterly Dividend Summary", "06/30/2026", "Complete"),
        )
    )
    body = (
        _heading("Reports")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + '<tr><td><font size="2">Select a report from the list.</font></td></tr></table>'
        + f'<table border="1" cellpadding="4" cellspacing="0"><tr>{header}</tr>{rows}</table>'
    )
    return _page("Reports", body)


def _admin_html() -> str:
    body = (
        _heading("Administration")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + f"<tr><td>{_red('You are not authorized to access this function.')}</td></tr>"
        + '<tr><td><font size="2"><a href="/admin/purge">Purge Member Records</a></font></td></tr>'
        + "</table>"
    )
    return _page("Administration", body)


def _purge_html(message: str | None = None) -> str:
    msg = f"<tr><td>{_red(message)}</td></tr>" if message else ""
    body = (
        _heading("Purge Member Records")
        + '<form method="post" action="/admin/purge">'
        + '<table border="1" cellpadding="6" cellspacing="0" bgcolor="#FFFFFF">'
        + msg
        + f"<tr><td>{_red('WARNING: This will permanently delete records.')}</td></tr>"
        + '<tr><td><input type="submit" value="Purge All Records"></td></tr>'
        + "</table></form>"
    )
    return _page("Purge Member Records", body)


def _notice_html(next_url: str) -> str:
    body = (
        _heading("Daily Notice")
        + '<table border="1" cellpadding="6" cellspacing="0" bgcolor="#FFFFCC">'
        + '<tr><td><font size="2">Reminder: Nightly batch processing begins at 9:00 PM CT.</font></td></tr>'
        + "</table><br>"
        + '<form method="post" action="/app/notice/ack">'
        + f'<input type="hidden" name="hidNext" value="{escape(next_url)}">'
        + '<input type="submit" value="Continue">'
        + "</form>"
    )
    return _page("Daily Notice", body)


def _error_html() -> str:
    body = (
        _heading("System Error")
        + '<table border="0" cellpadding="6" cellspacing="0">'
        + f"<tr><td>{_red('An unexpected error occurred. Reference #55-1017.')}</td></tr>"
        + "</table>"
    )
    return _page("System Error", body)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _parse_deposit(raw: str) -> float | None:
    """Accept legacy-friendly dollar input: '50', '50.00', '$1,250.50'."""
    cleaned = raw.strip().lstrip("$").replace(",", "").strip()
    if not _DEPOSIT_RE.match(cleaned):
        return None
    return float(cleaned)


def _next_subaccount(num: str, opened: set[str]) -> str:
    n = 1
    while f"{num}-C{n}" in opened:
        n += 1
    acct = f"{num}-C{n}"
    opened.add(acct)
    return acct


def _safe_app_path(path: str) -> bool:
    """Only relative in-app paths may be post-notice redirect targets."""
    if not (path == "/app" or path.startswith("/app/")):
        return False
    return "//" not in path and "\\" not in path


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "legacy-demo-not-a-secret"

    # Global mutable app state: current one-shot fault + sub-accounts opened.
    state: dict[str, Any] = {"fault": "none", "opened": set()}
    app.extensions["rote_state"] = state

    def _html(markup: str, status: int = 200) -> Response:
        resp = make_response(markup, status)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    @app.before_request
    def _legacy_gate() -> Response | None:
        path = request.path
        if path != "/app" and not path.startswith("/app/"):
            return None

        # One-shot fault injection: consume the pending fault, then act.
        fault = state["fault"]
        if fault != "none":
            state["fault"] = "none"
            if fault == "slow":
                time.sleep(_SLOW_SECONDS)
            elif fault == "error500":
                return _html(_error_html(), 500)
            elif fault == "timeout_next":
                session.clear()
            elif fault == "notice_next" and path != "/app/notice/ack":
                return _html(_notice_html(path))

        if "user" not in session:
            return _html(_login_html(SESSION_EXPIRED_MSG))
        return None

    # -- Authentication -----------------------------------------------------

    @app.get("/")
    def root() -> Response:
        return redirect("/app" if "user" in session else "/login")

    @app.get("/login")
    def login_form() -> Response:
        return _html(_login_html())

    @app.post("/login")
    def login_submit() -> Response:
        user = request.form.get("txtUser", "").strip()
        password = request.form.get("txtPass", "")
        if user in CREDENTIALS and CREDENTIALS[user] == password:
            session["user"] = user
            session["role"] = ROLE
            return redirect("/app")
        return _html(_login_html("Invalid credentials."))

    @app.get("/logout")
    def logout() -> Response:
        session.clear()
        return redirect("/login")

    # -- Chrome and business pages (inside the workarea iframe) -------------

    @app.get("/app")
    def chrome() -> Response:
        return _html(_chrome_html())

    @app.get("/app/home")
    def home() -> Response:
        return _html(_home_html())

    @app.get("/app/members")
    def member_search() -> Response:
        return _html(_search_html())

    @app.post("/app/members")
    def member_search_submit() -> Response:
        query = request.form.get("txtQ", "").strip()
        if not query.isdigit():
            return _html(_search_html("Member number must be numeric."))
        if query not in MEMBERS:
            return _html(_not_found_html())
        return redirect(f"/app/members/{query}")

    @app.get("/app/members/<num>")
    def member_profile(num: str) -> Response:
        member = MEMBERS.get(num)
        if member is None:
            return _html(_not_found_html())
        return _html(_profile_html(num, member))

    @app.get("/app/members/<num>/subaccount/new")
    def subaccount_form(num: str) -> Response:
        member = MEMBERS.get(num)
        if member is None:
            return _html(_not_found_html())
        return _html(_subaccount_form_html(num, member))

    @app.post("/app/members/<num>/subaccount/new")
    def subaccount_submit(num: str) -> Response:
        member = MEMBERS.get(num)
        if member is None:
            return _html(_not_found_html())
        form = request.form
        if "btnConfirm" in form:
            acct = _next_subaccount(num, state["opened"])
            return _html(_opened_html(num, acct))
        if "btnBack" in form:
            return _html(_subaccount_form_html(num, member))
        # First submit from the entry form: validate, then show review page.
        amount = _parse_deposit(form.get("txtDep", ""))
        if amount is None:
            return _html(_subaccount_form_html(
                num, member, "Initial deposit must be a dollar amount."))
        if amount < 25:
            return _html(_subaccount_form_html(
                num, member, "Minimum initial deposit is $25.00."))
        return _html(_review_html(
            num, form.get("selType", ""), f"{amount:,.2f}", form.get("selFund", "")))

    @app.post("/app/notice/ack")
    def notice_ack() -> Response:
        target = request.form.get("hidNext", "")
        if not _safe_app_path(target):
            target = "/app/home"
        return redirect(target)

    @app.get("/app/reports")
    def reports() -> Response:
        return _html(_reports_html())

    @app.get("/app/admin")
    def admin() -> Response:
        return _html(_admin_html())

    # -- Admin purge (allowlist-denial target; nothing is ever deleted) -----

    @app.get("/admin/purge")
    def purge_form() -> Response:
        return _html(_purge_html())

    @app.post("/admin/purge")
    def purge_submit() -> Response:
        return _html(_purge_html("Operation not permitted."))

    # -- Plumbing -----------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", mimetype="text/plain")

    @app.get("/debug/fault")
    def fault_get() -> Response:
        return Response(state["fault"], mimetype="text/plain")

    @app.post("/debug/fault")
    def fault_set() -> Response:
        value = request.form.get("fault", "")
        if value not in _FAULTS:
            return Response("unknown fault", status=400, mimetype="text/plain")
        state["fault"] = value
        return Response(value, mimetype="text/plain")

    return app


def main() -> None:
    port = int(os.environ.get("ROTE_TARGET_PORT", "7710"))
    create_app().run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
