"""The Notification model and the unread contract (notifications increment 1).

No producers, no page, no badge — those are increments 2, 3 and 4. What is
pinned here is the write side plus the read contract it has to support, in the
order each one would bite if it were wrong:

* **Each guard is proved against its own control.** Every ``notify()`` no-op is
  followed by the same call that *should* write, so a guard cannot pass by
  never being reached, and the whole function cannot pass by never writing.
  The self-action case is reachable, not theoretical: ``like_status`` and
  ``reply_to_status`` carry no self-guard of their own, so the only thing
  standing between your own like and your own badge is this function.
* **The block guard leaves no residue.** The row is absent rather than hidden,
  so lifting the block later cannot resurrect a notification for an
  interaction that was deliberately not delivered (R97(3)).
* **The block guard is one way.** ``blocks`` is ``symmetrical=False``; the
  guard reads the recipient's block list, not the actor's. Pinned so that
  making it symmetric someday is a decision and not a slip.
* **Mark-all-read is one UPDATE on the user row** — captured from the query
  log, because the R93 contract is about what the query *touches*, not only
  what it achieves.
* **The composite index exists in the database**, not merely in ``Meta``. The
  badge's cost claim (R94) rests on it, and a migration that lost it would
  otherwise surface as a slow page rather than a failed test.
* **The FK edges behave** — the recipient goes, the ledger goes; the actor
  goes, the record of them stays.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from reeltalk.core.models import Film, Status
from reeltalk.notifications.models import Notification, mark_all_read, notify

User = get_user_model()


@pytest.fixture
def alice(db):
    return User.objects.create_user(localname="alice", password="s3cretpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(localname="bob", password="s3cretpass")


@pytest.fixture
def dune(db):
    return Film.objects.create(title="Dune", year=2021)


@pytest.fixture
def post(db, alice, dune):
    return Status.objects.create(
        user=alice,
        film=dune,
        status_type=Status.Type.REVIEW,
        content="a review of Dune",
    )


@pytest.fixture
def admin_client(client, db):
    admin = User.objects.create_superuser(localname="root", password="s3cretpass")
    client.force_login(admin)
    return client


def test_notify_records_the_event(alice, bob, post):
    note = notify(alice, bob, Notification.Kind.LIKE, post)
    assert note is not None
    row = Notification.objects.get(pk=note.pk)
    assert row.recipient_id == alice.id
    assert row.actor_id == bob.id
    assert row.kind == "like"
    assert row.status_id == post.id
    assert row.created is not None


def test_the_kind_enum_holds_only_events_that_have_a_producer():
    # R90: a kind with no producer is not a spare slot for later, it is a
    # code path nobody can trigger and a test that can only be written
    # against a fiction. A fourth kind joins with its parser, not before it.
    # "mention" joined in mentions increment 4 for exactly that reason — the
    # parser (increments 1–2), the renderer and the outbound wire (3) all
    # landed first, so this set still names only reachable events.
    assert {kind.value for kind in Notification.Kind} == {
        "follow",
        "like",
        "reply",
        "mention",
    }


def test_notify_never_notifies_yourself(alice, bob, post):
    assert notify(alice, alice, Notification.Kind.LIKE, post) is None
    assert Notification.objects.count() == 0
    # Control: the identical call with a different actor writes, so the None
    # above is the guard and not a function that never writes at all.
    assert notify(alice, bob, Notification.Kind.LIKE, post) is not None
    assert Notification.objects.count() == 1


def test_notify_skips_a_recipient_who_is_a_remote_mirror(alice, bob, post):
    # The ordinary federated case, not the edge: handle_like aimed at a mirror
    # of another instance's post has status.user = a *remote* user. A row
    # addressed to a mirror is one no account here will ever read or clear.
    mirror = User.objects.create_user(
        localname="someone@other.example", password="s3cretpass", local=False
    )
    assert notify(mirror, bob, Notification.Kind.LIKE, post) is None
    assert Notification.objects.filter(recipient=mirror).count() == 0
    # Control: the same actor against a local recipient still writes.
    assert notify(alice, bob, Notification.Kind.LIKE, post) is not None
    assert Notification.objects.filter(recipient=alice).count() == 1


def test_notify_skips_a_recipient_who_blocked_the_actor(alice, bob, post):
    alice.blocks.add(bob)
    assert notify(alice, bob, Notification.Kind.LIKE, post) is None
    assert Notification.objects.count() == 0
    # The whole argument for write time: the block is lifted and nothing
    # appears, because there was never a row to stop hiding.
    alice.blocks.remove(bob)
    assert Notification.objects.count() == 0
    # Control: a fresh event after the unblock does arrive.
    assert notify(alice, bob, Notification.Kind.REPLY, post) is not None
    assert Notification.objects.count() == 1


def test_the_block_guard_reads_the_recipient_not_the_actor(alice, bob, post):
    # symmetrical=False cuts the other way: bob blocking alice says nothing
    # about whether alice may be told about bob.
    bob.blocks.add(alice)
    assert notify(alice, bob, Notification.Kind.LIKE, post) is not None
    assert Notification.objects.count() == 1


def test_mark_all_read_is_one_update_on_the_user_row(alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    notify(alice, bob, Notification.Kind.REPLY, post)
    with CaptureQueriesContext(connection) as captured:
        mark_all_read(alice)
    queries = [q["sql"] for q in captured.captured_queries]
    assert len(queries) == 1
    assert queries[0].startswith('UPDATE "social_user"')
    # R93: the cost of marking read must not scale with the unread count, so
    # the notification table is not written at all.
    assert "notifications_notification" not in queries[0]


def test_mark_all_read_empties_the_unread_set(alice, bob, post):
    notify(alice, bob, Notification.Kind.LIKE, post)
    assert Notification.unread_for(alice).count() == 1
    mark_all_read(alice)
    # Read through the in-memory instance, which is exactly how a view that
    # marks read and then renders the badge in one request would do it: had
    # mark_all_read written only to the database, this would still say one.
    assert Notification.unread_for(alice).count() == 0
    # A later event is unread again — the mark is a point in time, not a
    # switch that stays down.
    notify(alice, bob, Notification.Kind.REPLY, post)
    assert Notification.unread_for(alice).count() == 1


def test_unread_is_a_boundary_on_created(alice, bob, post):
    old = notify(alice, bob, Notification.Kind.LIKE, post)
    fresh = notify(alice, bob, Notification.Kind.REPLY, post)
    # Forced separation: two timezone.now() calls landing in the same
    # microsecond would make the boundary meaningless, and a test that
    # depends on the clock's luck is not a test.
    Notification.objects.filter(pk=old.pk).update(
        created=fresh.created - timedelta(hours=1)
    )
    User.objects.filter(pk=alice.pk).update(
        notifications_last_read=fresh.created - timedelta(minutes=1)
    )
    alice.refresh_from_db()
    assert [note.pk for note in Notification.unread_for(alice)] == [fresh.pk]


def test_a_new_account_starts_with_nothing_unread(alice):
    # The field defaults to signup time rather than NULL: "never read" and
    # "read as of the moment the account existed" are the same set, because
    # nothing can be addressed to a user before they exist — and the badge's
    # range query never has to handle a NULL bound.
    assert alice.notifications_last_read is not None
    assert Notification.unread_for(alice).count() == 0


def test_the_composite_index_exists_in_the_database(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            ["notifications_notification"],
        )
        defs = [row[0] for row in cursor.fetchall()]
    composite = [d for d in defs if "notif_recipient_created_idx" in d]
    assert composite, f"the composite index is missing from: {defs}"
    # Pinned to the column order, not just the name: recipient first, so the
    # badge's range scan on created runs inside the recipient prefix instead
    # of across every recipient's rows.
    assert composite[0].endswith("(recipient_id, created)"), composite[0]


def test_deleting_a_recipient_takes_their_ledger_and_a_deleted_actor_does_not(
    bob,
):
    carol = User.objects.create_user(localname="carol", password="s3cretpass")
    # Django clears the pk off a deleted instance, so the id has to be
    # captured before the delete or the query below cannot name her at all.
    carol_id = carol.pk
    notify(carol, bob, Notification.Kind.FOLLOW)
    notify(bob, carol, Notification.Kind.FOLLOW)
    carol.delete()
    # CASCADE on recipient: nobody inherits a notification nobody can read.
    assert Notification.objects.filter(recipient=carol_id).count() == 0
    # SET_NULL on actor: bob's record of being followed survives the
    # follower, with the actor rendered as gone.
    row = Notification.objects.get(recipient=bob)
    assert row.actor is None
    assert row.kind == "follow"


def test_a_hard_deleted_status_leaves_the_event_without_its_link(alice, bob, post):
    note = notify(alice, bob, Notification.Kind.LIKE, post)
    # Status.delete() is soft, so this models the hard path — the admin
    # changelist, whose delete goes through queryset delete. The like
    # happened whatever later became of the post.
    Status.objects.filter(pk=post.pk).delete()
    note.refresh_from_db()
    assert note.status is None
    assert note.kind == "like"


def test_str_says_who_did_it_and_says_someone_when_they_are_gone(alice, bob, post):
    # The SET_NULL actor still has to render as something. Increment 3 reads
    # this same distinction when it decides whether to draw the actor's link.
    note = notify(alice, bob, Notification.Kind.LIKE, post)
    assert str(note) == "bob like → alice"
    note.actor = None
    assert str(note) == "someone like → alice"


def test_the_ledger_is_readable_in_the_admin_but_has_no_add_form(
    admin_client, alice, bob, post
):
    notify(alice, bob, Notification.Kind.LIKE, post)
    # Readable: the admin is registered and the changelist answers. Without
    # this half, a blanket 403 everywhere would satisfy the next assertion.
    listing = admin_client.get("/admin/notifications/notification/")
    assert listing.status_code == 200
    # Not writable: a notification is the record of something that happened,
    # so there is no add form to fill in.
    assert admin_client.get("/admin/notifications/notification/add/").status_code == 403
    assert (
        admin_client.post(
            "/admin/notifications/notification/add/",
            {"recipient": alice.pk, "kind": "like"},
        ).status_code
        == 403
    )
    assert Notification.objects.count() == 1
