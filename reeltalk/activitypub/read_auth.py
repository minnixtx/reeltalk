"""What a signed machine request actually proves (visibility V2).

**The question this answers.** A followers-only post is *delivered* to the
author's remote followers the moment it is written, so their servers hold a copy.
Later one of those servers comes back to re-read the post at its own URL, and an
ordinary unauthenticated GET cannot say who is asking - so V1's rule (a post you
may not read 404s on the machine arm too, D9) would refuse a reader who is
entitled and hand them a not-found instead of a post they follow.

**What a signature buys.** A peer that signs its fetches signs them as a specific
account: Mastodon signs every ActivityPub GET and signs it as the account that
asked (``JsonLdHelper#fetch_resource_without_id_validation``, falling back to the
instance's representative actor). So a signed request can be resolved to an
actor, and the follow relation checked. That is the whole of it - a signature is
not a login, it is proof of *who is asking*, and everything after that is the
same audience rule the human pages apply.

**What it deliberately does not do.**

* It never fetches an unknown actor. The inbox resolves a first-contact sender by
  fetching their Person document because a delivery has to be attributed before
  it can be filed; a read endpoint that did the same would let anyone buy an
  outbound fetch from this instance by sending a made-up ``keyid``, on an
  endpoint with no delivery budget behind it. So resolution here is
  :func:`~reeltalk.activitypub.mirrors.resolve_known_actor` - an actor this
  instance already knows - and an unknown one is refused. A follower whose key we
  have never seen is a follower we have never delivered to, so nothing entitled
  is lost.
* It authorises the **wire document**, not the human page. The signature proves
  an actor to a *server*; the person reading in a browser is authenticated by
  their own session on their own instance, and this instance has no basis to
  render them a page. The caller checks content negotiation before asking.
* It carries no freshness window. A signature must cover the exact URL being
  asked for (``signatures.verify_request(require_target=True)``), which is what
  stops a signature made for one address being spent at another; replaying the
  *same* captured request would need the capture, and a capture already holds the
  response. A clock-dependent window on top of that would reject a peer whose
  clock drifts for no threat it closes.
"""

from reeltalk.core.models import Visibility

from .mirrors import resolve_known_actor
from .signatures import extract_key_id, verify_request


def may_fetch(request, status) -> bool:
    """May the actor behind this signed request read ``status`` over the wire?

    True only for a followers-only row whose followers include the verified
    requester - or where the requester *is* the author, which is the author's own
    server fetching on their behalf. Public rows never come through here; the view
    answers them before asking. A private row is refused outright: it was never
    delivered to anybody, so no remote account can be entitled to it.

    Every refusal is a False, and the view turns every False into the same 404
    the unsigned stranger gets. Which kind of refusal happened is not the
    requester's business, and telling them would confirm the post exists - the
    reason D9 chose 404 over 403 in the first place.
    """
    if status.visibility != Visibility.FOLLOWERS:
        return False
    key_id = extract_key_id(request)
    if not key_id:
        return False
    requester = resolve_known_actor(key_id, request)
    if requester is None or not requester.public_key:
        return False
    # ``require_target`` is not a formality. Without it a signature a peer made
    # for some other URL of ours would verify here too, because the covered
    # components are the sender's own declaration - and a verification that
    # passes for a request the key never covered is not an authorization.
    if not verify_request(request, requester.public_key, require_target=True):
        return False
    if requester.pk == status.user_id:
        return True
    return status.user.followers.filter(pk=requester.pk).exists()
