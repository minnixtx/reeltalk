"""Report how this instance resolves client addresses, and why.

Written for the deployer who has noticed that a limit fires too early (or
never fires) and has no way to find out what the app actually thinks their
users' addresses are. It answers three questions in one screenful, in an
order that matches how the diagnosis is actually done:

1. **What is configured?** The effective ``TRUSTED_PROXIES``, whether it is
   the shipped default or an override, and what ``SECURE_PROXY_SSL_HEADER``
   ended up as — because the second derives from the first and that pairing
   is the thing people get wrong.
2. **What would this chain resolve to?** With ``--peer`` and ``--xff`` you
   can paste a chain out of your proxy's own access log and get the walk
   printed hop by hop, trusted or not, with the reason for each step.
3. **What have we actually been resolving?** The recent per-source record
   from the verification token table — the same rows the throttle counts —
   collapsed into a distribution, so the "everyone looks like one address"
   failure is visible without waiting for a log aggregator.

Nothing here changes anything. It reads settings and one table.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from reeltalk.proxy_trust import is_trusted_proxy, resolve_chain
from reeltalk.social.models import EmailVerificationToken
from reeltalk.social.verify import RESEND_IP_LIMIT, RESEND_IP_WINDOW_MINUTES

LABEL_WIDTH = 26


class Command(BaseCommand):
    help = (
        "Report the effective proxy trust configuration, resolve a forwarded "
        "chain you supply, and summarise the client addresses this instance "
        "has recently resolved."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--peer",
            default="",
            help=(
                "The REMOTE_ADDR to resolve against — the address that "
                "actually opened the connection, as your proxy's access log "
                "records it."
            ),
        )
        parser.add_argument(
            "--xff",
            default="",
            help=(
                "An X-Forwarded-For value to walk, left to right as written. "
                "Requires --peer, because a chain means nothing without the "
                "peer that delivered it."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="How many recent verification sends to summarise (default 20).",
        )

    def handle(self, *args, **opts):
        # Nothing returned: Django's ``execute`` writes a truthy return value
        # straight to stdout, so an exit-code integer here would crash on
        # ``.endswith``. Bad input raises ``CommandError``, which is how the
        # framework means "stop, with this message and a non-zero status".
        if opts["xff"] and not opts["peer"]:
            raise CommandError(
                "--xff needs --peer: a forwarded chain is only meaningful "
                "against the address that delivered it."
            )
        self._report_config()
        if opts["peer"]:
            self._report_chain(opts["peer"], opts["xff"])
        self._report_recent(opts["limit"])

    # -- 1. what is configured -------------------------------------------

    def _report_config(self):
        proxies = list(settings.TRUSTED_PROXIES)
        default = list(getattr(settings, "PRIVATE_NETWORKS", []))
        origin = (
            "shipped default — the private ranges"
            if proxies == default
            else "set by the operator"
        )
        self.stdout.write(self.style.MIGRATE_HEADING("== configuration =="))
        if not proxies:
            self._line("TRUSTED_PROXIES", "(empty)")
            self.stdout.write(
                f"{'':{LABEL_WIDTH}}No proxy trusted: nothing forwarded is "
                "believed, so every request resolves to REMOTE_ADDR. If you "
                "do run behind a proxy, every per-address limit here counts "
                "your whole user base as one caller."
            )
        else:
            self._line("TRUSTED_PROXIES", ", ".join(proxies))
            self._line("", f"({origin})")
        self._line(
            "SECURE_PROXY_SSL_HEADER",
            repr(settings.SECURE_PROXY_SSL_HEADER)
            if settings.SECURE_PROXY_SSL_HEADER
            else "None (forwarded scheme not read)",
        )
        self._line(
            "per-source budget",
            f"{RESEND_IP_LIMIT} verification mails per "
            f"{RESEND_IP_WINDOW_MINUTES} minutes per resolved address",
        )
        self.stdout.write("")

    # -- 2. what would this chain resolve to -----------------------------

    def _report_chain(self, peer, xff):
        chain = [entry.strip() for entry in xff.split(",") if entry.strip()]
        self.stdout.write(
            self.style.MIGRATE_HEADING("== resolving the chain you supplied ==")
        )
        trust = "trusted proxy" if is_trusted_proxy(peer) else "not a trusted proxy"
        self._line("REMOTE_ADDR (peer)", f"{peer} — {trust}")
        self._line("X-Forwarded-For", xff or "(absent)")
        resolved, trace = resolve_chain(peer, chain)
        if not is_trusted_proxy(peer):
            self.stdout.write(
                f"{'  walk':{LABEL_WIDTH}}not taken — an untrusted peer "
                "cannot vouch for addresses further up the chain, so the "
                "header is not read at all."
            )
        elif not trace:
            self.stdout.write(
                f"{'  walk':{LABEL_WIDTH}}nothing to walk — no forwarded "
                "entries, so the peer is the answer."
            )
        else:
            self.stdout.write(f"{'  walk, right to left':{LABEL_WIDTH}}")
            for entry, verdict in trace:
                self._line(f"  {entry}", self._verdict_text(verdict), indent=2)
        self._line("resolved client IP", resolved or "(unknown)")
        self.stdout.write("")

    @staticmethod
    def _verdict_text(verdict: str) -> str:
        if verdict == "trusted":
            return "inside TRUSTED_PROXIES — skipped, the client is further left"
        if verdict == "client":
            return "not a trusted proxy — THIS IS THE CLIENT"
        return (
            "unparseable — the walk stops here and falls back to the peer, "
            "because a caller must not get to choose the throttle key"
        )

    # -- 3. what we have actually been resolving -------------------------

    def _report_recent(self, limit):
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"== recent verification sends (last {limit}) =="
            )
        )
        rows = list(
            EmailVerificationToken.objects.order_by("-created_at")[:limit].values_list(
                "created_at", "request_ip", "send_error"
            )
        )
        if not rows:
            self.stdout.write(
                "Nothing recorded yet. Ask for a verification mail (signup, "
                "or /account/verify/resend/), then run this again."
            )
            self.stdout.write("")
            return
        counted = []
        for created, ip, send_error in rows:
            stamp = timezone.localtime(created).strftime("%Y-%m-%d %H:%M:%S")
            state = "failed" if send_error else "sent"
            self.stdout.write(f"  {stamp}  {ip or '(no source recorded)':<45} {state}")
            if not send_error:
                counted.append(ip)
        distinct = {ip for ip in counted if ip}
        self.stdout.write("")
        self._line("sends counted by the throttle", str(len(counted)))
        self._line("distinct resolved addresses", str(len(distinct)))
        if not distinct:
            self.stdout.write(
                f"{'VERDICT':{LABEL_WIDTH}}Every recorded send came from a "
                "request with no source recorded, so the per-address axis "
                "never engaged. That means the sends came from a management "
                "command or a fixture rather than a request."
            )
        elif len(distinct) == 1:
            (only,) = distinct
            self.stdout.write(
                f"{'VERDICT':{LABEL_WIDTH}}Every counted send resolved to "
                f"one address ({only}). If that is not literally your only "
                "user, the real client addresses are not reaching us: check "
                "that your proxy sends X-Forwarded-For, and — if a CDN sits "
                "in front of it — add the CDN's ranges to TRUSTED_PROXIES."
            )
        else:
            self.stdout.write(
                f"{'VERDICT':{LABEL_WIDTH}}Distinct addresses are being "
                "resolved, so per-address limits key on distinct clients. "
                f"Each address may mint {RESEND_IP_LIMIT} mails per "
                f"{RESEND_IP_WINDOW_MINUTES} minutes before it is limited."
            )
        self.stdout.write("")

    # -- formatting -------------------------------------------------------

    def _line(self, label, value, indent=0):
        pad = " " * indent
        self.stdout.write(f"{pad + label:{LABEL_WIDTH}}{value}")
