import pytest

from custom_components.duke_energy.api.errors import (
    ApiChangedError,
    BlockedError,
    ClientRejectedError,
    InvalidGrantError,
    RequestRejectedError,
    TransientError,
    UnauthorizedError,
    classify_response,
    redact_body,
)

AKAMAI = (
    "<HTML><HEAD> <TITLE>Access Denied</TITLE> </HEAD><BODY> <H1>Access Denied</H1> "
    "You don't have permission to access \"http://api-v2.cma.duke-energy.app/login/"
    'auth-token" on this server.<P> Reference #18.1234567 errors.edgesuite.net</BODY></HTML>'
)


def classify(status, body, content_type="application/json", **kwargs):
    return classify_response(
        endpoint="test", status=status, content_type=content_type, body=body, **kwargs
    )


def test_success_is_none():
    assert classify(200, "{}") is None


@pytest.mark.parametrize(
    ("status", "body", "content_type", "expected"),
    [
        (403, AKAMAI, "text/html", BlockedError),
        (
            403,
            '{"Message":"User is not authorized to access this resource with an '
            'explicit deny in an identity-based policy"}',
            "application/json",
            ClientRejectedError,
        ),
        (
            403,
            '{"message":"Invalid key=value pair (missing equal-sign) in '
            'Authorization header"}',
            "application/json",
            ApiChangedError,
        ),
        (
            401,
            '{"returnCode":"401","messageText":"Invalid token"}',
            "application/json",
            UnauthorizedError,
        ),
        (
            500,
            '{"errors":[{"error":"Login is disabled at this time"}]}',
            "application/json",
            TransientError,
        ),
        (503, "", "text/plain", TransientError),
        (429, "{}", "application/json", TransientError),
        (404, '{"message":"Not Found"}', "application/json", ApiChangedError),
        (400, '{"message":"bad date"}', "application/json", RequestRejectedError),
    ],
)
def test_duke_api_classification(status, body, content_type, expected):
    assert isinstance(classify(status, body, content_type), expected)


def test_404_is_not_request_rejected():
    # Only the generic 4xx case counts as a possible history boundary.
    assert not isinstance(classify(404, "{}"), RequestRejectedError)


def test_auth0_invalid_grant():
    error = classify(
        403,
        '{"error":"invalid_grant","error_description":"Unknown or invalid refresh token."}',
        auth0=True,
    )
    assert isinstance(error, InvalidGrantError)


def test_auth0_html_block_is_blocked_not_reauth():
    assert isinstance(classify(403, AKAMAI, "text/html", auth0=True), BlockedError)


def test_retry_after_parsed():
    error = classify(429, "{}", headers={"Retry-After": "12"})
    assert isinstance(error, TransientError)
    assert error.retry_after == 12


def test_redact_body_masks_long_numbers():
    assert redact_body("account 123456789 ok 12345") == "account ****** ok 12345"
