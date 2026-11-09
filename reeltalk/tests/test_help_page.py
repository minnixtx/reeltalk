"""The member FAQ at ``/help/``.

The page is prose, and prose rots quietly: a route moves, a control is renamed,
and the sentence describing it stays. So almost everything here is structural
rather than a quote of the copy. The one place it does quote is the audience
section, whose sentences were reviewed once already (visibility V4) and must
survive any later edit of this page.

Two of the guards are about the owner's boundary for this page (2026-11-06): it
covers what a signed-in member can do and nothing about moderation or site
administration. That exclusion is a rule about the page's future, so it gets a
pin rather than a comment.
"""

import re

import pytest
from django.test import Client
from django.urls import resolve

from reeltalk.tests.members import member

HELP_URL = "/help/"

# The three audience markers. The wording around them is the owner's and moves;
# the fact that a member is told all three options is the part worth pinning
# here. test_visibility_moderation.py owns the visibility promises themselves.
AUDIENCE_LABELS = (
    "<strong>Public</strong>",
    "<strong>Followers</strong>",
    "<strong>Private</strong>",
)

# The rest of what the audience section promises (W3, 2026-11-08): that a list
# keeps the audience it was created with, that the Watchlist carries one audience
# for the whole shelf and re-files when the setting moves, that a finished film
# keeps the audience of its finish, and that nothing already delivered is taken
# back. The owner cut these sentences to shape, so they are quoted rather than
# paraphrased - and matched on collapsed whitespace, because the template wraps
# them and a line break in the source is not a different sentence.
AUDIENCE_PROMISES = (
    "Every review, list, and Watchlist add carries one of three audiences:",
    "A list takes its audience when you create it and keeps it.",
    "changing it re-files the films already on the shelf",
    "films you’ve marked watched keep the audience from when you finished them",
    "Narrowing an audience changes what this site shows from then on.",
    "It does not take back anything another server already received.",
)

# Staff surfaces and staff vocabulary. The page may say "moderators" in the one
# sentence about what happens to a report a member filed, and it may mention the
# site's admin tools in the sentence about what "Private" does not promise; it
# may not describe either surface as something a member can go to.
STAFF_VOCABULARY = (
    "/moderate/",
    "/admin/",
    "Moderation queue",
    "moderation queue",
    "Site settings",
    "is_staff",
    "is_moderator",
    "suspend",
    "signup_policy",
    "invite_scope",
)


def _toc(page: str) -> list[str]:
    """The anchors the contents box offers, in order."""
    box = page.split('<nav class="help-contents"', 1)
    assert len(box) == 2, "the page lost its contents box"
    inner = box[1].split("</nav>", 1)[0]
    return re.findall(r'href="#([^"]+)"', inner)


def _h2_ids(page: str) -> list[str]:
    """The group headings, taken from the sections that carry them.

    Read off ``aria-labelledby`` rather than any ``h2`` in the document, so the
    contents box's own heading cannot be mistaken for a group.
    """
    return re.findall(r'<section class="help-section" aria-labelledby="([^"]+)"', page)


def _h3_ids(page: str) -> list[str]:
    return re.findall(r'<h3 id="([^"]+)"', page)


def _ids(page: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', page))


def _hrefs(page: str) -> list[str]:
    """Every href on an anchor.

    Anchors only: a stylesheet ``<link>`` and a favicon carry hrefs too, and
    none of them are a destination the page is promising.
    """
    return re.findall(r'<a\b[^>]*?href="([^"]*)"', page)


def _internal(path: str) -> bool:
    return not path.startswith(("http://", "https://", "mailto:", "//"))


def _prose(page: str) -> str:
    """The page with every run of whitespace folded to one space.

    Quoted against the raw bytes, a sentence from a wrapped paragraph pins the
    wrapping too - the newline and the eight spaces of indentation in the middle
    of it - and rewrapping a line is not rewording it. Collapsing to single spaces
    keeps the words and drops the layout.
    """
    return " ".join(page.split())


@pytest.mark.django_db
def test_the_page_reads_for_a_visitor_and_for_a_member(db):
    """Both renders, because several answers branch on who is asking.

    Asserted on the reversed per-member URLs rather than on any sentence: the
    copy is the owner's and gets reworded, while the branch itself is the thing
    that can break. A visitor must not be handed a link to a page that is not
    theirs, and a member must actually be handed their own.
    """
    visitor = Client().get(HELP_URL)
    assert visitor.status_code == 200
    anonymous = visitor.content.decode()
    assert 'class="help-contents"' in anonymous
    assert "/user/reader/films/" not in anonymous

    reader = member(localname="reader")
    client = Client()
    client.force_login(reader)
    member_page = client.get(HELP_URL).content.decode()
    assert 'href="/user/reader/films/"' in member_page
    assert 'href="/user/reader/lists/"' in member_page


@pytest.mark.django_db
def test_all_three_audiences_are_named(db):
    """A member reading this page is told every audience there is.

    Half a list here is the one failure this section cannot be allowed: someone
    who reads Public and Followers but not Private decides what to write on the
    strength of it.
    """
    page = Client().get(HELP_URL).content.decode()
    for label in AUDIENCE_LABELS:
        assert label in page


@pytest.mark.django_db
def test_the_audience_promises_are_on_the_page(db):
    """The half of the section that is a promise rather than a list of options.

    ``test_all_three_audiences_are_named`` stops a member being told half the
    audiences; this stops them being told the audiences and nothing about what
    choosing one costs. The pinned sentences cover a list's fixed audience, the
    Watchlist shelf's one audience and its re-filing, the finished film that
    keeps its own, and the rule that narrowing an audience retracts nothing
    another server already holds.
    """
    page = _prose(Client().get(HELP_URL).content.decode())
    for sentence in AUDIENCE_PROMISES:
        assert sentence in page, f"the help page no longer says: {sentence!r}"


@pytest.mark.django_db
def test_every_link_the_page_offers_is_real(db):
    """The anti-drift guard.

    A help page that points at a moved route is worse than no help page, because
    it teaches the wrong shape of the site. Every internal href has to resolve,
    and every in-page anchor has to land on something that exists.
    """
    page = Client().get(HELP_URL).content.decode()
    ids = _ids(page)
    for href in _hrefs(page):
        if not _internal(href):
            continue
        target, _, fragment = href.partition("#")
        if fragment:
            assert fragment in ids, f"{href!r} points at no element on the page"
        if target in ("", HELP_URL):
            continue
        path = target.split("?", 1)[0]
        if not path.startswith("/"):
            path = "/" + path
        resolve(path)  # raises Resolver404 on a link that went stale


@pytest.mark.django_db
def test_the_contents_list_and_the_headings_agree(db):
    """The box lists the groups, and every group is in the box.

    The page lists groups rather than every question (owner's revision,
    2026-11-06), so the contract is one-to-one with the sections: a group missing
    from the box is a section nobody can jump to, and a listed anchor with no
    section is a dead link. The questions still each carry an ``id``, which is
    what makes a single answer linkable from elsewhere.
    """
    page = Client().get(HELP_URL).content.decode()
    toc = _toc(page)
    groups = _h2_ids(page)
    assert toc, "the contents box lists nothing"
    assert groups, "the page wrote no sections"
    assert set(toc) == set(groups), (
        f"in the box, not on the page: {sorted(set(toc) - set(groups))}; "
        f"on the page, not in the box: {sorted(set(groups) - set(toc))}"
    )
    assert len(toc) == len(set(toc)), "a group is listed twice"
    questions = _h3_ids(page)
    assert questions, "the page wrote no questions"
    assert len(questions) == len(set(questions)), "two questions share an id"


@pytest.mark.django_db
def test_the_page_stays_inside_a_member_s_business(db):
    """The owner's exclusion, as a pin rather than a promise.

    The FAQ is for the person using the instance. Moderation and administration
    are somebody else's job, and a page that starts explaining them is a page
    that has stopped being a member's.
    """
    page = Client().get(HELP_URL).content.decode()
    for word in STAFF_VOCABULARY:
        assert word not in page, f"the help page has started talking about {word!r}"


@pytest.mark.django_db
def test_the_welcome_page_hands_over_to_the_faq(db):
    """The third door (D5): the four-step page points at the reference page.

    Asserted on the sentence rather than on any ``href="/help/"``, because the
    footer already carries that link on every page and would pass this test on
    its own.
    """
    page = Client().get("/welcome/").content.decode()
    assert 'The <a href="/help/">Help page</a> answers' in page
