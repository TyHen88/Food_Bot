"""Person-identity helpers shared by the invoice API and the AI assistant.

Order items and invoice details identify a person in two different ways:

    {"user_id": "123456", "name": "Dara"}   # modern rows
    {"user_id": "",       "name": "Dara"}   # guests + rows written before
                                            # per-entry user_id existed

Anything that aggregates money per person has to treat those as ONE human,
otherwise a member's spending is split across two buckets and every total
built from it is too low. Both `bot/api/invoices.py::_my_amount` and
`bot/ai.py` used to re-derive this matching separately (and disagreed —
one lowercased names, the other didn't), so it lives here now.

Name comparison is normalized: zero-width characters (which do turn up in
names and dish names copied out of Telegram), collapsed whitespace, and
case-folding.
"""

import re
from typing import Any, Dict, Iterable, Optional, Set, Tuple

# U+200B/C/D ZERO WIDTH SPACE/NON-JOINER/JOINER, U+2060 WORD JOINER,
# U+FEFF BOM. These survive copy-paste out of Telegram and make two
# visually identical names compare unequal.
ZERO_WIDTH = "​‌‍⁠﻿"
_ZERO_WIDTH_MAP = dict.fromkeys(map(ord, ZERO_WIDTH), None)
_WHITESPACE = re.compile(r"\s+")


def strip_invisible(value: Any) -> str:
    """Drop zero-width characters and collapse whitespace, preserving case."""
    return _WHITESPACE.sub(" ", str(value or "").translate(_ZERO_WIDTH_MAP)).strip()


def norm_name(value: Any) -> str:
    """Comparison key for a display name: invisible-stripped, case-folded."""
    return strip_invisible(value).casefold()


def name_variants(
    *,
    username: Any = "",
    first_name: Any = "",
    last_name: Any = "",
    full_name: Any = "",
) -> Set[str]:
    """Normalized display names a person's legacy (user_id-less) rows may
    carry: their @username, first name, and full name.

    The bare first name is included because that is what older rows stored,
    but callers must only use these as a FALLBACK for entries that have no
    user_id — two members sharing a first name would otherwise merge. See
    `build_uid_index`, which refuses to resolve ambiguous names.
    """
    first = strip_invisible(first_name)
    last = strip_invisible(last_name)
    candidates = {
        username,
        first,
        f"{first} {last}".strip(),
        full_name,
    }
    if not first and full_name:
        # Callers that only know a combined "First Last" still get the
        # first-name variant that legacy rows may have used.
        candidates.add(strip_invisible(full_name).split(" ")[0])
    return {norm_name(c) for c in candidates if norm_name(c)}


def is_same_person(entry_uid: Any, entry_name: Any, uid: Any, names: Set[str]) -> bool:
    """Does an order-item / invoice-detail entry belong to `uid`?

    An entry that carries a user_id is matched on it alone — a name match
    must never override a user_id that says otherwise.
    """
    euid = str(entry_uid or "").strip()
    if euid:
        target = str(uid or "").strip()
        return bool(target) and euid == target
    return norm_name(entry_name) in names


def build_uid_index(entries: Iterable[Tuple[Any, Any]]) -> Dict[str, Optional[str]]:
    """Map normalized name → the user_id that name belongs to.

    `entries` is (user_id, name) pairs from every source being aggregated.
    Only entries that HAVE a user_id contribute. A name seen with two
    different user_ids maps to None (ambiguous) so `person_key` leaves those
    entries un-merged rather than folding two people together.
    """
    index: Dict[str, Optional[str]] = {}
    for uid, name in entries:
        u = str(uid or "").strip()
        key = norm_name(name)
        if not u or not key:
            continue
        if key in index and index[key] != u:
            index[key] = None  # ambiguous — same name, different people
        else:
            index.setdefault(key, u)
    return index


def person_key(entry_uid: Any, entry_name: Any,
               uid_index: Optional[Dict[str, Optional[str]]] = None) -> str:
    """Stable grouping key for one entry.

    Prefers the entry's own user_id; for entries without one, recovers the
    user_id from `uid_index` (built by `build_uid_index`) so legacy rows land
    in the same bucket as that person's modern rows. Falls back to the
    normalized name when the id is unknown or the name is ambiguous.
    """
    euid = str(entry_uid or "").strip()
    if euid:
        return euid
    key = norm_name(entry_name)
    if uid_index:
        resolved = uid_index.get(key)
        if resolved:
            return resolved
    return key or "guest"
