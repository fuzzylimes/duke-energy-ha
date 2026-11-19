import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.duke_energy.api import (
    Account,
    ApiChangedError,
    Auth0Client,
    AuthExpiredError,
    DukeEnergyClient,
    Meter,
    TokenManager,
    TokenSet,
    TransientError,
)
from custom_components.duke_energy.api.client import (
    parse_accounts,
    parse_meters,
    windows,
)
from custom_components.duke_energy.api.const import (
    ACCOUNT_LIST_URL,
    AUTH_TOKEN_URL,
    TOKEN_URL,
    USAGE_GRAPH_URL,
)

from ..helpers import (
    EMAIL,
    FALL_BACK_HOURS,
    INTERNAL_ID,
    SERIAL,
    day_rows,
    make_id_token,
    rows_for_hours,
)
from .conftest import json_body, sequence

UA = "Duke%20Energy/9999 CFNetwork/1.0 Darwin/1.0"
TZ = ZoneInfo("America/New_York")

ACCOUNT = Account(
    account_number="9000000001",
    src_sys_cd="ISU",
    src_acct_id="9000000001",
    src_acct_id2="",
    primary_bp_number="BP1",
    related_bp_number="BP2",
    zip_code="27601",
)
METER = Meter(
    serial_number=SERIAL,
    service_type="ELECTRIC",
    agreement_active_date=date(2020, 1, 15),
    agreement_end_date=date(9999, 12, 31),
    meter_cert_date=date(2020, 1, 1),
    account=ACCOUNT,
)


def make_client(transport, now=None):
    tokens = TokenSet("r1", make_id_token(exp=time.time() + 1800), time.time() + 1800)
    auth0 = Auth0Client(transport, lambda: UA)
    manager = TokenManager(transport, auth0, tokens, user_agent=lambda: UA)
    return DukeEnergyClient(transport, manager, lambda: UA, now=now)


@pytest.fixture
def api_token(mock_http):
    mock_http.post(
        str(AUTH_TOKEN_URL), json={"access_token": "api", "expires_in": 1800}
    )


def test_parse_accounts_new_route_shape():
    payload = {
        "relatedBPNumber": "REL",
        "accounts": [
            {
                "accountNumber": "111",
                "primaryBPNumber": "P-new",
                "primaryBpNumber": "P-old",
                "serviceAddressParsed": {"zipCode": "28202"},
            }
        ],
    }
    (account,) = parse_accounts(payload)
    assert account.primary_bp_number == "P-new"
    assert account.related_bp_number == "REL"
    assert account.src_sys_cd == "ISU"
    assert account.src_acct_id == "111"
    assert account.src_acct_id2 == ""
    assert account.zip_code == "28202"


def test_parse_accounts_keeps_supplied_source_ids_and_old_spelling():
    payload = {
        "relatedBpNumber": "REL",
        "accounts": [
            {
                "accountNumber": "111",
                "primaryBpNumber": "P-old",
                "srcSysCd": "CIS",
                "srcAcctId": "S1",
                "srcAcctId2": "S2",
            }
        ],
    }
    (account,) = parse_accounts(payload)
    assert (account.src_sys_cd, account.src_acct_id, account.src_acct_id2) == (
        "CIS",
        "S1",
        "S2",
    )
    assert account.primary_bp_number == "P-old"
    assert account.related_bp_number == "REL"


def test_parse_accounts_rejects_wrong_shape():
    with pytest.raises(ApiChangedError):
        parse_accounts({"nope": []})


def test_parse_meters_keeps_serial_verbatim():
    payload = {
        "meterInfo": [
            {
                "serviceType": "ELECTRIC",
                "serialNum": SERIAL,
                "agreementActiveDate": "2020-01-15",
                "agreementEndDate": "9999-12-31",
                "meterCertificationDate": "2020-01-01",
            },
            {"serviceType": "GAS", "serialNum": "G1"},
            {"serviceType": "ELECTRIC"},
        ]
    }
    meters = parse_meters(payload, ACCOUNT)
    assert [m.serial_number for m in meters] == [SERIAL, "G1"]
    assert meters[0].agreement_active_date == date(2020, 1, 15)
    assert meters[1].agreement_active_date is None


def test_windows():
    assert windows(date(2026, 1, 1), date(2026, 1, 1)) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]
    spans = windows(date(2026, 1, 1), date(2026, 3, 5))
    assert spans[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert spans[-1][1] == date(2026, 3, 5)
    assert all((b - a).days < 31 for a, b in spans)
    assert windows(date(2026, 1, 2), date(2026, 1, 1)) == []


def test_usage_body(transport):
    fixed = datetime(2026, 11, 18, 14, 3, 27, 123000, tzinfo=TZ)
    client = make_client(transport, now=lambda tz: fixed)
    body = client.usage_body(METER, date(2026, 10, 1), date(2026, 10, 31))
    assert body["date"] == "2026-10-01T14:03:27.123-04:00"
    assert body["startDate"] == "10/01/2026"
    assert body["endDate"] == "10/31/2026"
    assert body["agrmtStartDt"] == "01/15/2020"
    assert body["intervalFrequency"] == "HOURLY"
    assert body["periodType"] == "DAY"
    assert body["meterSerialNumber"] == SERIAL
    assert body["zipCode"] == "27601"


async def test_get_accounts_sends_identity(transport, mock_http, api_token):
    mock_http.get(str(ACCOUNT_LIST_URL), json={"accounts": [], "relatedBPNumber": ""})
    client = make_client(transport)
    assert await client.async_get_accounts() == []
    url = mock_http.mock_calls[-1][1]
    assert url.query["email"] == EMAIL
    assert url.query["internalUserID"] == INTERNAL_ID


async def test_data_call_retries_once_on_401(transport, mock_http, api_token):
    mock_http.post(str(TOKEN_URL), json={"id_token": make_id_token()})
    mock_http.get(
        str(ACCOUNT_LIST_URL),
        side_effect=sequence(
            {"status": 401, "json": {"message": "Unauthorized"}},
            {"json": {"accounts": []}},
        ),
    )
    client = make_client(transport)
    assert await client.async_get_accounts() == []


async def test_data_call_second_401_is_auth_expired(transport, mock_http, api_token):
    mock_http.post(str(TOKEN_URL), json={"id_token": make_id_token()})
    mock_http.get(str(ACCOUNT_LIST_URL), status=401, json={"message": "Unauthorized"})
    with pytest.raises(AuthExpiredError):
        await make_client(transport).async_get_accounts()


async def test_transient_is_retried_then_raised(transport, mock_http, api_token):
    mock_http.get(str(ACCOUNT_LIST_URL), status=503, text="busy")
    with pytest.raises(TransientError):
        await make_client(transport).async_get_accounts()
    account_calls = [
        c for c in mock_http.mock_calls if c[1].with_query(None) == ACCOUNT_LIST_URL
    ]
    assert len(account_calls) == 3


async def test_hourly_exact_window(transport, mock_http, api_token):
    mock_http.post(str(USAGE_GRAPH_URL), json={"usageArray": day_rows() + day_rows()})
    result = await make_client(transport).async_get_hourly(
        METER, date(2026, 6, 1), date(2026, 6, 2)
    )
    assert len(result.readings) == 48
    assert result.readings[0].start == datetime(2026, 6, 1, 4, tzinfo=UTC)
    assert result.rejected_days == []


async def test_hourly_mismatch_refetches_by_day(transport, mock_http, api_token):
    short_day = day_rows()
    del short_day[7]
    mock_http.post(
        str(USAGE_GRAPH_URL),
        side_effect=sequence(
            {"json": {"usageArray": day_rows() + day_rows()}},  # 48 rows for 3 days
            {"json": {"usageArray": day_rows()}},
            {"json": {"usageArray": short_day}},
            {"json": {"usageArray": [{"date": "bogus", "usage": "1"}]}},
        ),
    )
    result = await make_client(transport).async_get_hourly(
        METER, date(2026, 6, 1), date(2026, 6, 3)
    )
    assert len(result.readings) == 24 + 23
    assert result.missing_hours == 1
    assert result.rejected_days == [date(2026, 6, 3)]
    day_bodies = [
        call[2] for call in mock_http.mock_calls if call[1] == USAGE_GRAPH_URL
    ][1:]
    assert [b["startDate"] for b in day_bodies] == [
        "06/01/2026",
        "06/02/2026",
        "06/03/2026",
    ]
    assert all(b["startDate"] == b["endDate"] for b in day_bodies)


async def test_hourly_invalid_shape(transport, mock_http, api_token):
    mock_http.post(str(USAGE_GRAPH_URL), json={"something": "else"})
    with pytest.raises(ApiChangedError):
        await make_client(transport).async_get_hourly(
            METER, date(2026, 6, 1), date(2026, 6, 1)
        )


async def test_hourly_counts_negative_values(transport, mock_http, api_token):
    rows = day_rows()
    rows[2]["usage"] = "-0.4"
    mock_http.post(str(USAGE_GRAPH_URL), json={"usageArray": rows})
    result = await make_client(transport).async_get_hourly(
        METER, date(2026, 6, 1), date(2026, 6, 1)
    )
    assert len(result.readings) == 23
    assert result.invalid_values == 1
    assert json_body(mock_http)["meterSerialNumber"] == SERIAL


async def test_missing_identity_is_recovered_by_refresh(
    transport, mock_http, api_token
):
    tokens = TokenSet("r1", "", 0.0)  # e.g. migrated without an ID token
    auth0 = Auth0Client(transport, lambda: UA)
    manager = TokenManager(transport, auth0, tokens, user_agent=lambda: UA)
    assert manager.identity is None
    mock_http.post(str(TOKEN_URL), json={"id_token": make_id_token()})
    mock_http.get(str(ACCOUNT_LIST_URL), json={"accounts": []})
    client = DukeEnergyClient(transport, manager, lambda: UA)
    assert await client.async_get_accounts() == []
    url = next(
        c[1] for c in mock_http.mock_calls if c[1].with_query(None) == ACCOUNT_LIST_URL
    )
    assert url.query["internalUserID"] == INTERNAL_ID


async def test_window_containing_fall_back_needs_one_request(
    transport, mock_http, api_token
):
    # 2026-10-19..2026-11-18, the live case: 30 normal days plus Duke's 24-row
    # fall-back day. It must line up without day-by-day re-fetching.
    start, end = date(2026, 10, 19), date(2026, 11, 18)
    rows = []
    day = start
    while day <= end:
        rows += (
            rows_for_hours(FALL_BACK_HOURS) if day == date(2026, 11, 1) else day_rows()
        )
        day += timedelta(days=1)
    mock_http.post(str(USAGE_GRAPH_URL), json={"usageArray": rows})
    result = await make_client(transport).async_get_hourly(METER, start, end)
    usage_calls = [c for c in mock_http.mock_calls if c[1] == USAGE_GRAPH_URL]
    assert len(usage_calls) == 1
    assert len(result.readings) == 31 * 24
    assert result.missing_hours == 1  # the repeated 01:00 EST hour
    assert result.rejected_days == []
