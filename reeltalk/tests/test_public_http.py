"""Untrusted URLs cannot open private sockets, even through DNS or redirects."""

import socket
from unittest.mock import patch

import pytest
import requests
import responses
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from urllib3.exceptions import NewConnectionError

from reeltalk import public_http


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_private_destination_never_opens_socket(address):
    answers = [(socket_family(address), 1, 6, "", (address, 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        with pytest.raises(NewConnectionError):
            public_http.PublicHTTPSConnection("remote.example")._new_conn()
        connect.assert_not_called()


def socket_family(address):
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def test_dns_rebinding_cannot_change_checked_destination():
    answers = [(2, 1, 6, "", ("1.1.1.1", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers) as resolve,
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        public_http.PublicHTTPSConnection("remote.example")._new_conn()
        resolve.assert_called_once()
        assert connect.call_args.args[0] == ("1.1.1.1", 443)


def test_mixed_public_and_private_dns_is_rejected():
    answers = [(2, 1, 6, "", (ip, 443)) for ip in ["1.1.1.1", "10.0.0.1"]]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        with pytest.raises(NewConnectionError):
            public_http.PublicHTTPSConnection("remote.example")._new_conn()
        connect.assert_not_called()


def test_redirect_to_private_destination_is_blocked():
    with responses.RequestsMock() as mock:
        mock.add(
            responses.GET,
            "https://remote.example/",
            status=302,
            headers={"Location": "http://127.0.0.1/"},
        )
        mock.add_passthru("http://127.0.0.1/")
        with pytest.raises(requests.ConnectionError):
            public_http.get("https://remote.example/", timeout=1)


def test_signed_post_does_not_follow_redirect():
    with responses.RequestsMock() as mock:
        mock.add(
            responses.POST,
            "https://remote.example/inbox",
            status=307,
            headers={"Location": "https://elsewhere.example/"},
        )
        assert public_http.post("https://remote.example/inbox").status_code == 307
        assert len(mock.calls) == 1


def test_document_size_cap():
    with (
        responses.RequestsMock() as mock,
        patch.object(public_http, "MAX_RESPONSE_BYTES", 10),
    ):
        mock.add(responses.GET, "https://remote.example/", body="x" * 11)
        with pytest.raises(requests.RequestException, match="size cap"):
            public_http.get("https://remote.example/")


@override_settings(FEDERATION_ALLOWED_NETWORKS=["192.168.1.0/24"])
def test_allowlisted_lan_peer_opens_socket():
    answers = [(socket.AF_INET, 1, 6, "", ("192.168.1.85", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        public_http.PublicHTTPSConnection("peer.lan")._new_conn()
        assert connect.call_args.args[0] == ("192.168.1.85", 443)


@override_settings(FEDERATION_ALLOWED_NETWORKS=["10.0.0.0/8"])
def test_private_destination_outside_the_allowlist_stays_blocked():
    answers = [(socket.AF_INET, 1, 6, "", ("192.168.1.85", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        with pytest.raises(NewConnectionError):
            public_http.PublicHTTPSConnection("peer.lan")._new_conn()
        connect.assert_not_called()


@override_settings(FEDERATION_ALLOWED_NETWORKS=["192.168.1.0/24"])
def test_metadata_ip_stays_blocked_alongside_a_lan_allowlist():
    answers = [(socket.AF_INET, 1, 6, "", ("169.254.169.254", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        with pytest.raises(NewConnectionError):
            public_http.PublicHTTPSConnection("remote.example")._new_conn()
        connect.assert_not_called()


@override_settings(FEDERATION_ALLOWED_NETWORKS=["not-a-cidr"])
def test_invalid_cidr_names_the_setting():
    answers = [(socket.AF_INET, 1, 6, "", ("192.168.1.85", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection"),
    ):
        with pytest.raises(ImproperlyConfigured, match="FEDERATION_ALLOWED_NETWORKS"):
            public_http.PublicHTTPSConnection("peer.lan")._new_conn()


def test_scoped_ipv6_literal_is_refused_not_crashed():
    # A scoped literal must not escape the OSError contract as a ValueError.
    answers = [(socket.AF_INET6, 1, 6, "", ("fe80::1%eth0", 443))]
    with (
        patch("socket.getaddrinfo", return_value=answers),
        patch("reeltalk.public_http.create_connection") as connect,
    ):
        with pytest.raises(NewConnectionError):
            public_http.PublicHTTPSConnection("remote.example")._new_conn()
        connect.assert_not_called()
