"""Two tenants running the same underlying vendor product.

Same handlers, same templates — only this config differs. That is the point:
it stands in for the assignment's "hundreds of tenants, ~20 apps each, many
running the same vendor product configured and branded differently", and lets
us demonstrate one recorded artifact being applied to a second variant.

The differences are chosen to be exactly the ones that break naive automation:
different route nouns, different query-param names, different form field order,
different labels, different theme.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    key: str
    name: str            # institution branding in the masthead
    product_version: str # same vendor product, different release
    prefix: str          # route prefix; "" for the root-mounted tenant
    noun: str            # "Member" / "Customer"
    noun_plural: str
    collection: str      # path segment: "members" / "customers"
    search_param: str    # query param name for the ID search
    name_param: str      # query param name for the surname search
    theme: str           # css class on <body>
    # Field order on the new-sub-account form. Same fields, different sequence,
    # so a positional/tab-order strategy breaks and a labelled one survives.
    subaccount_fields: tuple[str, ...]

    def path(self, *parts: str) -> str:
        tail = "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))
        return f"{self.prefix}/{tail}" if tail else (self.prefix or "/")


NORTHSTAR = Tenant(
    key="northstar",
    name="NorthStar Core Banking",
    product_version="CoreTeller 7.2.1",
    prefix="",
    noun="Member",
    noun_plural="Members",
    collection="members",
    search_param="memberNumber",
    name_param="lastName",
    theme="theme-northstar",
    subaccount_fields=("nickname", "deposit", "purpose", "statements"),
)

PINEBANK = Tenant(
    key="pinebank",
    name="Pinebank Servicing",
    product_version="CoreTeller 6.9.4",
    prefix="/pb",
    noun="Customer",
    noun_plural="Customers",
    collection="customers",
    search_param="q",
    name_param="surname",
    theme="theme-pinebank",
    subaccount_fields=("purpose", "nickname", "statements", "deposit"),
)

TENANTS: dict[str, Tenant] = {t.key: t for t in (NORTHSTAR, PINEBANK)}


def by_prefix(path: str) -> Tenant:
    """Longest-prefix match, so /pb/... resolves before the root tenant."""
    for t in sorted(TENANTS.values(), key=lambda t: len(t.prefix), reverse=True):
        if t.prefix and path.startswith(t.prefix + "/"):
            return t
        if t.prefix and path == t.prefix:
            return t
    return NORTHSTAR
