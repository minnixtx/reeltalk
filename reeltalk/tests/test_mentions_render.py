"""The mention renderer and the flag-scoped sanitizer (mentions increment 2).

Two halves that only work as a pair: the renderer decides what a mention
looks like, and the sanitizer decides whether any of it survives. Widening
either one alone yields a mention that renders as inert text with nothing in
either file explaining why.

Every guard carries its own control, per the notifications increment-1
shape, and the sanitizer tests carry the lesson increment 1 learned the hard
way -- **a negative assertion can pass because some downstream stage
declined, not because the guard under test ever fired.** So the mention
span's attributes are asserted present in the *same* sanitize call as an
ordinary anchor's asserted absent, against the *same* allowlist. The
widening is proven load-bearing by that pair; neither half proves it alone.

The M-a half matters as much as the rendering half. ``render_markdown`` has
five production callers and the flag defaults off, so what is pinned here is
not merely that mentions work but that the three surfaces M-a declined --
member bios, film descriptions, TMDB overviews -- are still rendered by
exactly the code that rendered them before this increment existed.
"""

import mistune
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from reeltalk.core.forms import FilmForm
from reeltalk.core.models import Film, Status
from reeltalk.core.utils import render_markdown, sanitize_html
from reeltalk.social.models import LinkDomain

User = get_user_model()

ALLOWED_SITE = "themoviedb.org"
MIRROR_HANDLE = "minnix@upallnight.minnix.dev"


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def mirror(db):
    return User.objects.create_user(
        localname=MIRROR_HANDLE,
        password="s3cretpass",
        local=False,
        actor_url="https://upallnight.minnix.dev/users/minnix",
    )


@pytest.fixture
def allowed_domain(db):
    LinkDomain.objects.create(domain=ALLOWED_SITE)


def login(user):
    client = Client()
    client.force_login(user)
    return client


# --- the sanitizer widening: one call, both halves --------------------------


@pytest.mark.django_db
def test_the_widening_keeps_the_span_attrs_and_strips_both_from_an_ordinary_anchor(
    allowed_domain,
):
    """The headline pair, in a single ``sanitize_html`` call.

    The span keeps ``class`` and ``data-link`` because of the new allowance;
    the member's own anchor keeps its href and loses both. Asserting only
    the span would not distinguish the allowance from a sanitizer that
    simply stopped stripping anything, so the anchor is the control that
    says the gate is still a gate.
    """
    user_anchor = (
        f'<a href="https://{ALLOWED_SITE}/movie/1"'
        ' class="mention" data-link="true">the movie</a>'
    )
    html = sanitize_html(
        '<span class="mention" data-link="true">@alice</span> ' + user_anchor,
        mentions=True,
    )
    assert '<span class="mention" data-link="true">@alice</span>' in html
    # The anchor survives with its href and nothing else.
    assert f'<a href="https://{ALLOWED_SITE}/movie/1">the movie</a>' in html
    # Exactly one of each attribute in the whole document, and both belong to
    # the span -- the anchor's copies were stripped.
    assert html.count("class=") == 1
    assert html.count("data-link=") == 1


@pytest.mark.django_db
def test_span_is_denied_entirely_without_the_flag():
    """The tag widening is the flag's, not the sanitizer's.

    Same input, no flag: the span is not a permitted tag at all and comes
    back escaped. This is the control that makes the previous test's span
    assertion about the widening rather than about bleach being permissive.
    """
    html = sanitize_html('<span class="mention" data-link="true">@alice</span>')
    assert "<span" not in html
    assert "&lt;span" in html


@pytest.mark.django_db
def test_the_span_attributes_are_matched_by_exact_value():
    """``class``/``data-link`` survive only at the values we emit.

    Without the exact-value check the widening would be "any class, any
    data-* on a span", which is a general attribute allowance wearing a
    mention's clothes. Each wrong value is its own control against the
    right one.
    """
    assert (
        sanitize_html('<span class="other" data-link="true">x</span>', mentions=True)
        == '<span data-link="true">x</span>'
    )
    assert (
        sanitize_html('<span class="mention" data-link="yes">x</span>', mentions=True)
        == '<span class="mention">x</span>'
    )
    # Control: the exact pair, both kept.
    assert (
        sanitize_html('<span class="mention" data-link="false">x</span>', mentions=True)
        == '<span class="mention" data-link="false">x</span>'
    )


@pytest.mark.django_db
def test_a_span_carries_no_other_attribute_even_on_the_mention_path():
    """The allowance names two attributes; ``id`` is not one of them."""
    assert (
        sanitize_html('<span class="mention" id="zz" style="x">y</span>', mentions=True)
        == '<span class="mention">y</span>'
    )


@pytest.mark.django_db
def test_the_site_relative_profile_href_is_admitted_only_on_the_mention_path():
    """``LinkDomain.is_allowed("")`` is False, so this arm is the only reason
    a mention's own link survives. Off the mention path it is stripped
    exactly as it always was -- which is what keeps a remote Person
    document's relative href from starting to point at our host.
    """
    kept = sanitize_html('<a href="/user/alice/">alice</a>', mentions=True)
    assert 'href="/user/alice/"' in kept

    denied = sanitize_html('<a href="/user/alice/">alice</a>')
    assert "href" not in denied
    assert "<a>alice</a>" == denied


@pytest.mark.django_db
def test_relative_hrefs_that_are_not_profile_paths_stay_denied_on_the_mention_path():
    """The new arm matches a profile shape, not "any root-relative path".

    Without the shape check the widening would admit every relative href in
    the document, including ones that walk into the admin.
    """
    html = sanitize_html(
        '<a href="/admin/users/">a</a> <a href="/images/posters/x.jpg">b</a> '
        '<a href="/user/../admin/">c</a> <a href="/user/alice/">d</a>',
        mentions=True,
    )
    assert "/admin/" not in html
    assert "/images/" not in html
    assert 'href="/user/alice/"' in html


# --- the renderer: the flag reaches the template ----------------------------


@pytest.mark.django_db
def test_the_flag_is_what_reaches_the_template(alice):
    """One input, two flags, two documents.

    With the flag the template renders the linked form; without it the same
    handle is the plain text it always was. If the flag were dropped anywhere
    between ``render_markdown`` and the template, one of these two
    assertions is the one that goes red.
    """
    linked = render_markdown("thanks @alice", mentions=True)
    assert '<span class="mention" data-link="true">' in linked
    assert 'href="/user/alice/"' in linked

    plain = render_markdown("thanks @alice")
    assert plain == "<p>thanks @alice</p>"
    assert "<span" not in plain


@pytest.mark.django_db
def test_an_unresolved_handle_renders_plain_text_not_a_link(alice):
    """A handle we cannot resolve is not an error and is not a link.

    The span still marks it as having parsed as a mention, with
    ``data-link="false"`` and no anchor. The control is the resolved handle
    in the same document, which must have the anchor -- otherwise this test
    would pass on a renderer that never links anything.
    """
    html = render_markdown("@alice greets @nosuchmember", mentions=True)
    assert '<a href="/user/alice/">@alice</a>' in html
    assert '<span class="mention" data-link="false">@nosuchmember</span>' in html
    assert "@nosuchmember</a>" not in html
    # Only one anchor in the document, and it is alice's.
    assert html.count("<a ") == 1


@pytest.mark.django_db
def test_a_mention_inside_a_link_label_does_not_nest_anchors(alice, allowed_domain):
    """The nested-anchor trap, pinned.

    mistune renders a link's label by recursing over its children *before*
    it calls ``link()``, so a ``text()``-only override would put a mention
    anchor inside the member's own anchor. The resulting HTML looks fine in
    a browser and breaks anything that parses it, which is exactly why this
    is asserted rather than reasoned about.

    ``alice`` has to be a real account for this to test anything: with the
    handle unresolvable the renderer would emit plain text for a different
    reason and the guard would go unproven.
    """
    html = render_markdown(
        f"[look at @alice here](https://{ALLOWED_SITE}/movie/1)", mentions=True
    )
    assert html.count("<a") == 1
    assert "mention" not in html
    assert "look at @alice here" in html
    # Control: the same handle outside the label does become a mention.
    control = render_markdown(
        f"[label](https://{ALLOWED_SITE}/movie/1) and @alice", mentions=True
    )
    assert control.count("<a") == 2
    assert '<span class="mention" data-link="true">' in control


@pytest.mark.django_db
def test_a_mention_inside_an_image_label_does_not_nest_anchors(alice):
    """The same guard covers image alt text.

    ``<img>`` is not an allowed tag so bleach escapes the whole thing
    anyway, but the renderer must not have built a span inside the alt
    string to begin with -- the guard's job is to not emit it, not to rely
    on a downstream stage to clean it up.
    """
    html = render_markdown("![alt text @alice](/p.png)", mentions=True)
    assert "mention" not in html


@pytest.mark.django_db
def test_code_spans_and_fenced_blocks_stay_plain(alice):
    """No guard needed here, asserted anyway so it stays that way.

    mistune tokenises these as ``codespan`` / ``block_code`` and their
    renderer methods never recurse through ``text()``. The control is the
    prose mention in the same string.
    """
    html = render_markdown(
        "use `@alice` for the handle\n\n```\n@alice in a fence\n```\n\nbut @alice here",
        mentions=True,
    )
    assert "<code>@alice</code>" in html
    assert "@alice in a fence" in html
    assert "<code>@alice</span>" not in html
    # The fenced and inline copies are not linked; only the prose one is.
    assert html.count('href="/user/alice/"') == 1


@pytest.mark.django_db
def test_emails_and_url_userinfo_are_not_linked(alice):
    """The parser's lookbehind property, re-proven through the renderer.

    The parser refusing to *report* these does not make the renderer refuse
    to *emit* them; they share only the regex, and this walks the render
    path.
    """
    html = render_markdown(
        "mail alice@example.com or see https://x.io/@alice but @alice replies",
        mentions=True,
    )
    assert html.count('href="/user/alice/"') == 1
    assert '<a href="mailto' not in html


@pytest.mark.django_db
def test_the_href_comes_from_the_stored_localname_not_the_typed_casing(alice):
    """``@ALICE`` resolves case-insensitively but must link to ``/user/alice/``.

    The visible text stays what the member typed; only the href is
    normalised. Linking with the typed casing would 404, because the stored
    row is ``alice``.
    """
    html = render_markdown("shout @ALICE", mentions=True)
    assert 'href="/user/alice/"' in html
    assert ">@ALICE</a>" in html


@pytest.mark.django_db
def test_a_mirror_handle_links_to_the_mirror_localname(mirror):
    """A remote handle's href carries the full ``user@host`` localname.

    The route serves that path verbatim, so the href must too.
    """
    html = render_markdown(f"echo @{MIRROR_HANDLE}", mentions=True)
    assert f'href="/user/{MIRROR_HANDLE}/"' in html
    assert f">@{MIRROR_HANDLE}</a>" in html


@pytest.mark.django_db
def test_every_occurrence_links_including_repeats(alice):
    """Repeats are separate spans; the de-dup is a storage concern, not a
    rendering one."""
    html = render_markdown("@alice, @alice and @alice", mentions=True)
    assert html.count('href="/user/alice/"') == 3


# --- M-a: the declined surfaces are byte-identical -------------------------


DECLINED_SAMPLES = [
    "a bio mentioning @alice and @bob",
    "TMDB overview: see @alice for more",
    "[a link](https://themoviedb.org/movie/1) and @alice",
    "`@alice` in code, @alice in prose, alice@example.com in mail",
    "@alice",
]


@pytest.mark.parametrize("source", DECLINED_SAMPLES)
@pytest.mark.django_db
def test_the_default_path_is_byte_identical_to_the_pre_increment_path(source):
    """The declined callers get the *old code path*, not a similar one.

    ``render_markdown(s)`` must equal the literal expression this function
    was before the flag existed. Byte equality rather than "no span present"
    because the requirement is that nothing at all changed for these
    surfaces, including the parts unrelated to mentions.
    """
    reference = sanitize_html(mistune.html(source).strip())
    assert render_markdown(source) == reference
    assert '<span class="mention"' not in render_markdown(source)


@pytest.mark.django_db
def test_the_film_description_form_does_not_link_mentions():
    """M-a at the call site: a film description containing a handle."""
    form = FilmForm(
        data={
            "title": "Blade Runner",
            "description": "a film about @alice",
            "genres": "",
            "directors": "",
            "cast": "",
        }
    )
    assert form.is_valid(), form.errors
    assert "mention" not in form.cleaned_data["description"]
    assert "a film about @alice" in form.cleaned_data["description"]


@pytest.mark.django_db
def test_the_tmdb_field_builder_does_not_link_mentions():
    """M-a at the call site: third-party text nobody chose to have mention.

    A TMDB overview that happens to contain ``@someone`` must not become a
    link into our member list.
    """
    from reeltalk.core.tmdb import film_fields_from_tmdb

    fields = film_fields_from_tmdb(
        {
            "title": "X",
            "overview": "synopsis mentioning @alice",
            "release_date": "1982-06-25",
        }
    )
    assert '<span class="mention"' not in fields["description"]
    assert "synopsis mentioning @alice" in fields["description"]


@pytest.mark.django_db
def test_the_bio_route_does_not_link_mentions(alice, bob):
    """M-a at the call site, through the real route.

    ``ProfileForm``'s help text invites markdown in a bio, so this is the
    surface where a member would most reasonably expect a mention to work
    and where M-a says it does not.
    """
    response = login(alice).post(
        "/preferences/profile/",
        {"display_name": "Alice", "summary": "hello @bob over here"},
    )
    assert response.status_code == 302
    alice.refresh_from_db()
    assert alice.summary == "<p>hello @bob over here</p>"
    assert "mention" not in alice.summary


@pytest.mark.django_db
def test_remote_person_html_still_loses_class_and_relative_hrefs():
    """The other consumer of ``sanitize_html`` keeps deny-by-default.

    ``refresh_mirror_profile`` feeds a remote Person document's ``summary``
    through this function with no flag. If the widening leaked here, a
    remote bio's ``href="/user/x"`` would start pointing at our host
    instead of theirs, and a remote span would arrive dressed as one of our
    mentions.
    """
    remote = (
        '<p>remote bio with <a href="/user/alice/">a relative link</a> and '
        '<span class="mention" data-link="true">a fake mention</span></p>'
    )
    cleaned = sanitize_html(remote)
    assert 'href="/user/alice/"' not in cleaned
    assert "<span" not in cleaned
    assert "a relative link" in cleaned
    assert "a fake mention" in cleaned


# --- the wiring: the two status sites opt in, through their real routes ----


@pytest.mark.django_db
def test_the_reply_route_stores_a_linked_mention(alice, bob):
    """The reply write site passes ``mentions=True``.

    Asserted on the stored column rather than on a response body, because
    the stored HTML is the artifact every later surface reads.
    """
    film = Film.objects.create(title="Alien")
    parent = Status.objects.create(
        user=alice, film=film, status_type="review", rating=4, content="<p>scary</p>"
    )
    response = login(bob).post(
        f"/status/{parent.pk}/reply/", {"content": "agreed, ask @alice why"}
    )
    assert response.status_code == 200
    reply = Status.objects.get(reply_parent=parent, user=bob)
    assert '<a href="/user/alice/">@alice</a>' in reply.content
    assert 'data-link="true"' in reply.content


@pytest.mark.django_db
def test_the_finish_flow_stores_a_linked_mention(alice, bob):
    """The review write site passes ``mentions=True``."""
    film = Film.objects.create(title="Alien")
    response = login(alice).post(
        f"/film/{film.pk}/watched/",
        {"rating": "4", "content": "loved it, ask @bob"},
    )
    assert response.status_code == 302
    review = Status.objects.get(user=alice, film=film)
    assert '<a href="/user/bob/">@bob</a>' in review.content
    assert 'data-link="true"' in review.content
