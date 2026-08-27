"""Static demo data for the Cornerstone Federal Credit Union teller app.

Every record here is obviously fake: SSNs use the invalid 000- prefix and the
only credential is a shared demo login. Nothing in this module is real PII.
"""

from __future__ import annotations

from typing import TypedDict

# (account number, account type, status, current balance)
Account = tuple[str, str, str, str]


class Member(TypedDict):
    name: str
    status: str
    joined: str
    ssn: str
    accounts: list[Account]


MEMBERS: dict[str, Member] = {
    "12345": {
        "name": "Margaret Ellison",
        "status": "Active",
        "joined": "03/14/2011",
        "ssn": "000-12-3456",
        "accounts": [
            ("12345-S1", "Regular Savings", "Active", "$4,982.17"),
            ("12345-D1", "Checking", "Active", "$1,203.55"),
        ],
    },
    "12346": {
        "name": "Daniel Okafor",
        "status": "Active",
        "joined": "07/02/2015",
        "ssn": "000-23-4567",
        "accounts": [
            ("12346-S1", "Regular Savings", "Active", "$310.09"),
        ],
    },
    "20001": {
        # Edge case: checking only, no savings account.
        "name": "Priya Raman",
        "status": "Active",
        "joined": "01/19/2020",
        "ssn": "000-34-5678",
        "accounts": [
            ("20001-D1", "Checking", "Active", "$8,455.00"),
        ],
    },
    "30777": {
        "name": "Chen Wei",
        "status": "Dormant",
        "joined": "11/30/2008",
        "ssn": "000-45-6789",
        "accounts": [
            ("30777-S1", "Regular Savings", "Dormant", "$12.40"),
        ],
    },
    "41200": {
        "name": "Rosa Delgado",
        "status": "Active",
        "joined": "05/05/2018",
        "ssn": "000-56-7890",
        "accounts": [
            ("41200-S1", "Regular Savings", "Active", "$77,150.33"),
            ("41200-D1", "Checking", "Active", "$942.10"),
        ],
    },
}

CREDENTIALS: dict[str, str] = {"tclark": "spring2026-demo"}

ROLE: str = "teller"
