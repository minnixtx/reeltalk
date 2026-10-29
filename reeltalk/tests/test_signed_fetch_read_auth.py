"""What a signed machine request may read (visibility V2).

V1 made the ActivityPub arm of a post answer 404 to every non-public row, which
was right when only the author could ever read one. Followers-only breaks that:
the post was *delivered* to the author's remote followers, and when one of their
servers comes back to re-read it, a bare GET cannot say who is asking. The owner
chose to let a signature answer that question rather than ship a "Followers"
that behaves like "only me" across the wire.

The peer's side of this is not a guess: Mastodon signs every ActivityPub fetch and
signs it as the account that asked, so a follower's server arrives holding proof
of the follower. These tests pin both halves of that - the signature has to
verify, and the account it verifies has to be someone the audience includes.

Two refusals are the load-bearing ones and both are easy to get wrong:

* **a signature that never named this URL is not an authorization.** The covered
  components are the sender's own declaration, so a signature over ``host`` and
  ``date`` alone verifies perfectly well - for attribution. Spending it to open a
  post is a different claim, and ``require_target`` is what refuses it. The test
  below signs a *valid* signature that omits the target and asserts it verifies
  without the flag and refuses with it, so the refusal is the rule and not a
  broken signature.
* **resolving a key must not buy an outbound fetch.** The inbox fetches an unknown
  actor because a delivery has to be attributed; a read endpoint that did the same
  would let anyone make this instance dial out on demand, on a door with no
  delivery budget behind it. So an unknown ``keyid`` is refused here, and the test
  asserts the fetch path was never reached.
"""

import base64
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import Client, RequestFactory

from reeltalk.activitypub import crypto, mirrors, signatures
from reeltalk.core.models import Film, Status, Visibility
from reeltalk.social.models import User
from reeltalk.tests.members import member

HOST = "testserver"
PASSWORD = "s3cretpass"
AP_ACCEPT = "application/activity+json"
AP = {"HTTP_ACCEPT": AP_ACCEPT}


@pytest.fixture
def author(db):
    return member(localname="author", password=PASSWORD)


@pytest.fixture
def film(db):
    return Film.objects.create(title="Arrival", year=2016)


@pytest.fixture
def review(db, author, film):
    """A followers-only review: the row this whole file is about."""
    return Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        content="<p>only my followers read this</p>",
        rating=4,
        visibility=Visibility.FOLLOWERS,
    )


def _rsa_pair() -> tuple[str, str]:
    """An RSA pair, because that is the format current Mastodon signs with."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


def _remote(localname: str, public_pem: str) -> User:
    """A remote mirror with a key we know - the state a real follower is in."""
    return User.objects.create(
        localname=f"{localname}@remote.example",
        local=False,
        actor_url=f"https://remote.example/users/{localname}",
        inbox_url=f"https://remote.example/users/{localname}/inbox",
        public_key=public_pem,
    )


def _meta(headers: dict) -> dict:
    """Turn response-style header names into ``Client.get`` META kwargs."""
    return {
        f"HTTP_{name.upper().replace('-', '_')}": value
        for name, value in headers.items()
    }


def _legacy_headers(
    private_pem: str, key_id: str, url: str, *, cover_target: bool = True
) -> dict:
    """Old-draft (rsa-sha256) GET signature headers - the Mastodon shape.

    ``cover_target=False`` drops the ``(request-target)`` pseudo-header from the
    list *and* from the signed string, which is what a sender that signed only
    ``host`` and ``date`` would really send. The signature is valid either way;
    the question is whether it authorizes *this* URL.
    """
    parsed = urlparse(url)
    date = format_datetime(datetime.now(UTC), usegmt=True)
    listed = ["(request-target)", "host", "date"] if cover_target else ["host", "date"]
    lines = [f"host: {parsed.netloc}", f"date: {date}"]
    if cover_target:
        lines.insert(0, f"(request-target): get {parsed.path or '/'}")
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    signature = key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "Date": date,
        "Signature": ",".join(
            [
                f'keyId="{key_id}"',
                'algorithm="rsa-sha256"',
                f'headers="{" ".join(listed)}"',
                f'signature="{base64.b64encode(signature).decode()}"',
            ]
        ),
    }


def _fetch(url: str, headers: dict, *, accept: str | None = AP_ACCEPT):
    meta = _meta(headers)
    # The signature covers ``host``, and Django's test client does not put a Host
    # header on the request the way a real socket does - without this the
    # verifier is handed a request whose signed ``host`` line names nothing, and
    # every case below fails for a reason no real peer ever hits.
    meta["HTTP_HOST"] = HOST
    if accept:
        meta["HTTP_ACCEPT"] = accept
    return Client().get(urlparse(url).path, **meta)


# --- the entitlement ---------------------------------------------------------


@pytest.mark.django_db
def test_an_unsigned_fetch_of_a_followers_post_still_404s(db, review):
    """V1's door stays shut to the anonymous: the signature is what opens it."""
    assert Client().get(f"/status/{review.id}/", **AP).status_code == 404


@pytest.mark.django_db
def test_a_follower_that_signs_its_fetch_is_served_the_note(db, author, review):
    """The case the increment exists for, in the format the peer actually uses."""
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    url = f"http://{HOST}/status/{review.id}/"
    headers = _legacy_headers(private_pem, f"{follower.actor_url}#main-key", url)
    response = _fetch(url, headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == url
    assert "only my followers read this" in body["content"]


@pytest.mark.django_db
def test_a_follower_signing_rfc9421_is_served_too(db, author, review):
    """Both live formats authorize; the double-knock peer must not be locked out."""
    private_pem, public_pem = crypto.generate_keypair()
    follower = _remote("rfc", public_pem)
    author.followers.add(follower)
    url = f"http://{HOST}/status/{review.id}/"
    headers = signatures.sign_request(
        "GET", url, private_pem, key_id=f"{follower.actor_url}#main-key"
    )
    assert _fetch(url, headers).status_code == 200


@pytest.mark.django_db
def test_a_key_that_does_not_follow_the_author_is_refused(db, review):
    private_pem, public_pem = _rsa_pair()
    stranger = _remote("stranger", public_pem)
    url = f"http://{HOST}/status/{review.id}/"
    assert (
        _fetch(
            url, _legacy_headers(private_pem, f"{stranger.actor_url}#main-key", url)
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_a_follower_cannot_fetch_a_private_post(db, author, film):
    """A private row was never delivered to anybody, so nobody is entitled."""
    private_review = Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        content="<p>mine</p>",
        rating=4,
        visibility=Visibility.PRIVATE,
    )
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    url = f"http://{HOST}/status/{private_review.id}/"
    assert (
        _fetch(
            url, _legacy_headers(private_pem, f"{follower.actor_url}#main-key", url)
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_a_public_post_needs_no_signature_at_all(db, author, film):
    """V2 must not add friction to the audience that was already open."""
    public = Status.objects.create(
        user=author,
        film=film,
        status_type=Status.Type.REVIEW,
        content="<p>open</p>",
        rating=4,
    )
    assert Client().get(f"/status/{public.id}/", **AP).status_code == 200


@pytest.mark.django_db
def test_a_signed_follower_get_does_not_get_the_human_page(db, author, review):
    """The signature proves an actor to a server, not a person to a browser.

    Serving HTML on the strength of a machine signature would hand a session-grade
    page to whoever replayed the signature, so the signed door is the wire
    document and nothing else.
    """
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    url = f"http://{HOST}/status/{review.id}/"
    assert (
        _fetch(
            url,
            _legacy_headers(private_pem, f"{follower.actor_url}#main-key", url),
            accept="text/html",
        ).status_code
        == 404
    )


# --- what makes a signature an authorization ---------------------------------


@pytest.mark.django_db
def test_a_valid_signature_that_omits_the_target_authorizes_nothing(db, author, review):
    """The sharp edge of ``require_target``: the signature verifies, and still loses.

    Signing only ``host`` and ``date`` is a signature a peer could have made for
    any URL of ours. Without the target rule it verifies cleanly, which is enough
    to answer "who sent this" and is not enough to answer "may this actor read
    this post".
    """
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    url = f"http://{HOST}/status/{review.id}/"
    headers = _legacy_headers(
        private_pem, f"{follower.actor_url}#main-key", url, cover_target=False
    )

    # Same headers, two questions: attribution passes, authorization refuses.
    request = RequestFactory().get(urlparse(url).path)
    request.META["HTTP_HOST"] = HOST
    for name, value in headers.items():
        request.META[f"HTTP_{name.upper().replace('-', '_')}"] = value
    assert signatures.verify_request(request, public_pem) is True
    assert signatures.verify_request(request, public_pem, require_target=True) is False
    assert _fetch(url, headers).status_code == 404


@pytest.mark.django_db
def test_a_signature_made_for_another_url_is_not_reusable(db, author, review):
    """A signature for one address cannot be spent at another."""
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    other = f"http://{HOST}/user/{author.localname}/"
    assert (
        _fetch(
            f"http://{HOST}/status/{review.id}/",
            _legacy_headers(private_pem, f"{follower.actor_url}#main-key", other),
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_an_unknown_keyid_is_refused_without_dialing_out(db, review, monkeypatch):
    """No first-contact fetch on a read door: a made-up keyid must cost nothing.

    The inbox fetches an unknown actor because a delivery cannot be filed without
    attribution. A GET has no such need, and without the rule anyone could make
    this instance fetch on demand through an endpoint with no delivery budget.
    """

    def no_fetch(*args, **kwargs):
        raise AssertionError("a read must not fetch an unknown actor")

    monkeypatch.setattr(mirrors, "fetch_person_document", no_fetch)
    private_pem, public_pem = _rsa_pair()
    url = f"http://{HOST}/status/{review.id}/"
    assert (
        _fetch(url, _legacy_headers(private_pem, f"{url}#main-key", url)).status_code
        == 404
    )


@pytest.mark.django_db
def test_a_suspended_actor_is_refused(db, author, review):
    """R102's chokepoint covers the new door for free: a suspended actor never
    resolves, so their signature is worth nothing here."""
    private_pem, public_pem = _rsa_pair()
    follower = _remote("follower", public_pem)
    author.followers.add(follower)
    from django.utils import timezone

    follower.suspended_at = timezone.now()
    follower.save()
    url = f"http://{HOST}/status/{review.id}/"
    assert (
        _fetch(
            url, _legacy_headers(private_pem, f"{follower.actor_url}#main-key", url)
        ).status_code
        == 404
    )
