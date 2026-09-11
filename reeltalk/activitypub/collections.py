"""ActivityPub OrderedCollection + pagination (M4 increment 3, R41).

Shared by the outbox, followers, and following collections (and shelves when
their wire type lands): a collection URL served without ``?page`` returns the
``OrderedCollection`` document (``totalItems`` + ``first``/``last`` page
URLs); the same URL with ``?page=N`` returns one ``OrderedCollectionPage``
(``partOf`` + 1-based ``startIndex`` + that page's ``items``). Built fresh
against the ActivityPub spec (R7) — no AP library.
"""

# Items per collection page (R41).
PAGE_SIZE = 20

_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]


def parse_page(request) -> int:
    """The requested 1-based page number; absent or malformed means page 1."""
    try:
        page = int(request.GET.get("page", "1"))
    except (TypeError, ValueError):
        return 1
    return max(1, page)


def last_page(total: int) -> int:
    """The 1-based index of the final page (at least one, even when empty)."""
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def collection_document(collection_url: str, total: int) -> dict:
    """The ``OrderedCollection`` document — no items; pages carry them."""
    return {
        "@context": _CONTEXT,
        "id": collection_url,
        "type": "OrderedCollection",
        "totalItems": total,
        "first": f"{collection_url}?page=1",
        "last": f"{collection_url}?page={last_page(total)}",
    }


def page_document(collection_url: str, items: list[dict], start_index: int) -> dict:
    """One ``OrderedCollectionPage`` — a slice of a collection's items.

    ``start_index`` is the 1-based index of the first item on this page (the
    ActivityPub convention); the page's own id is its ``?page=`` URL.
    """
    page_number = (start_index - 1) // PAGE_SIZE + 1
    return {
        "@context": _CONTEXT,
        "id": f"{collection_url}?page={page_number}",
        "type": "OrderedCollectionPage",
        "partOf": collection_url,
        "startIndex": start_index,
        "items": items,
    }
