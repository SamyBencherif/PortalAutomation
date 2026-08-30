"""Deterministic member data.

Certain member IDs always produce certain business outcomes. That is on purpose:
the replay engine must treat "no such member" and "account frozen" as legitimate
results rather than crashes, so those conditions must be reachable with no setup
at all. See OUTCOME_* below.

The clock is fixed. Nothing in this module is random.
"""

from dataclasses import dataclass, field, replace
from datetime import date

TODAY = date(2026, 3, 2)

# Business outcomes a member record can force. These are *results*, not failures.
OUTCOME_OK = "ok"
OUTCOME_FROZEN = "frozen"
OUTCOME_RESTRICTED = "restricted"
OUTCOME_MAX_SUBACCOUNTS = "max_subaccounts"
OUTCOME_NEEDS_OVERRIDE = "needs_override"

MAX_SUBACCOUNTS = 3


@dataclass
class Account:
    number: str
    kind: str          # "Savings" | "Checking" | "Sub-Savings"
    balance: str       # string, not float: this is a UI fixture, not a ledger
    status: str        # "Active" | "Frozen" | "Closed"
    opened: str


@dataclass
class Member:
    member_no: str
    first: str
    last: str
    # Fake, but shaped like the real thing. These render on the detail screen so
    # the automation's redaction rules have something to actually bite on.
    ssn: str
    dob: str
    branch: str
    outcome: str = OUTCOME_OK
    accounts: list[Account] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def sub_count(self) -> int:
        return sum(1 for a in self.accounts if a.kind == "Sub-Savings")


def _seed() -> dict[str, Member]:
    return {
        m.member_no: m
        for m in [
            Member(
                "10001", "Dana", "Reyes", "412-55-9080", "1984-07-19", "Riverside",
                OUTCOME_OK,
                [
                    Account("SAV-10001-01", "Savings", "4,182.55", "Active", "2019-04-02"),
                    Account("CHK-10001-01", "Checking", "1,043.19", "Active", "2019-04-02"),
                ],
            ),
            Member(
                "10002", "Chris", "Okonkwo", "298-11-4477", "1991-01-30", "Riverside",
                OUTCOME_FROZEN,
                [
                    Account("SAV-10002-01", "Savings", "812.00", "Frozen", "2021-09-15"),
                    Account("CHK-10002-01", "Checking", "97.42", "Active", "2021-09-15"),
                ],
            ),
            Member(
                "10003", "Sam", "Alvi", "551-30-2210", "1977-11-08", "Northgate",
                OUTCOME_RESTRICTED,
                [Account("SAV-10003-01", "Savings", "22,904.10", "Active", "2011-02-28")],
            ),
            Member(
                "10004", "Jordan", "Pike", "330-89-1265", "1995-06-04", "Northgate",
                OUTCOME_MAX_SUBACCOUNTS,
                [
                    Account("SAV-10004-01", "Savings", "660.75", "Active", "2020-08-11"),
                    Account("SAV-10004-02", "Sub-Savings", "150.00", "Active", "2022-01-05"),
                    Account("SAV-10004-03", "Sub-Savings", "75.00", "Active", "2023-03-19"),
                    Account("SAV-10004-04", "Sub-Savings", "0.00", "Active", "2024-10-01"),
                ],
            ),
            Member(
                "10005", "Riley", "Chen", "607-42-3318", "1969-12-22", "Lakeview",
                OUTCOME_NEEDS_OVERRIDE,
                [
                    Account("SAV-10005-01", "Savings", "318,220.04", "Active", "2004-05-17"),
                    Account("CHK-10005-01", "Checking", "12,880.63", "Active", "2004-05-17"),
                ],
            ),
            # Three Lees: a surname search here is ambiguous on purpose, so the
            # agent has to disambiguate rather than blindly take row one.
            Member(
                "10006", "Morgan", "Lee", "744-19-5502", "1988-03-14", "Lakeview",
                OUTCOME_OK,
                [Account("SAV-10006-01", "Savings", "2,015.88", "Active", "2018-06-30")],
            ),
            Member(
                "10007", "Alex", "Lee", "744-19-6613", "1990-09-02", "Riverside",
                OUTCOME_OK,
                [Account("SAV-10007-01", "Savings", "509.12", "Active", "2020-02-14")],
            ),
            Member(
                "10008", "Priya", "Lee", "744-19-7724", "1982-05-25", "Northgate",
                OUTCOME_OK,
                [Account("CHK-10008-01", "Checking", "3,301.77", "Active", "2015-11-09")],
            ),
        ]
    }


class MemberStore:
    """In-process member data. Resettable, so runs cannot leak into each other."""

    def __init__(self) -> None:
        self._members: dict[str, Member] = _seed()
        self._confirmation_seq = 0
        # (member_no, nickname.lower()) -> (account_number, confirmation).
        # A replayed commit is the normal case for this system, so a repeat of
        # work that already succeeded has to be reportable as such instead of
        # silently opening a second account.
        self._committed: dict[tuple[str, str], tuple[str, str]] = {}

    def reset(self) -> None:
        self._members = _seed()
        self._confirmation_seq = 0
        self._committed = {}

    def get(self, member_no: str) -> Member | None:
        return self._members.get(member_no.strip())

    def search_by_name(self, term: str) -> list[Member]:
        t = term.strip().lower()
        if not t:
            return []
        hits = [
            m for m in self._members.values()
            if t in m.last.lower() or t in m.first.lower()
        ]
        return sorted(hits, key=lambda m: m.member_no)

    def next_confirmation(self) -> str:
        """Deterministic counter, not a timestamp or a UUID.

        Two identical runs separated by a reset must produce identical
        confirmation numbers, otherwise replay comparison is meaningless.
        """
        self._confirmation_seq += 1
        return f"CNF-{self._confirmation_seq:06d}"

    @staticmethod
    def _commit_key(member_no: str, nickname: str) -> tuple[str, str]:
        return (member_no.strip(), nickname.strip().lower())

    def find_commit(self, member_no: str, nickname: str) -> tuple[str, str] | None:
        """The (account_number, confirmation) of an identical earlier commit."""
        return self._committed.get(self._commit_key(member_no, nickname))

    def add_subaccount(
        self, member_no: str, nickname: str, deposit: str
    ) -> tuple[Account, str]:
        """Open a sub-account and mint its confirmation in one step.

        Both are minted here, together with the dedupe entry, so a committed
        account can never exist without a confirmation for a replayed commit to
        be handed back.
        """
        m = self._members[member_no]
        seq = len(m.accounts) + 1
        acct = Account(
            number=f"SAV-{member_no}-{seq:02d}",
            kind="Sub-Savings",
            balance=deposit,
            status="Active",
            opened=TODAY.isoformat(),
        )
        # dataclass list is shared with the seed copy only via replace(); append
        # is safe because _seed() rebuilds the list on every reset.
        m.accounts.append(acct)
        confirmation = self.next_confirmation()
        self._committed[self._commit_key(member_no, nickname)] = (acct.number, confirmation)
        return acct, confirmation


__all__ = [
    "TODAY", "MAX_SUBACCOUNTS", "Account", "Member", "MemberStore",
    "OUTCOME_OK", "OUTCOME_FROZEN", "OUTCOME_RESTRICTED",
    "OUTCOME_MAX_SUBACCOUNTS", "OUTCOME_NEEDS_OVERRIDE", "replace",
]
