"""Repair issues for problems re-auth can't fix (docs/DESIGN.md §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    ISSUE_API_CHANGED,
    ISSUE_BLOCKED,
    ISSUE_CLIENT_REJECTED,
    ISSUE_METADATA_MISMATCH,
    ISSUE_NEW_METER,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

ENTRY_ISSUES = (ISSUE_BLOCKED, ISSUE_CLIENT_REJECTED, ISSUE_API_CHANGED)


def _issue_id(key: str, scope: str) -> str:
    return f"{key}_{scope}"


def create_entry_issue(
    hass: HomeAssistant,
    entry_id: str,
    key: str,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Raise one of the per-entry connection issues."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(key, entry_id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=key,
        translation_placeholders=placeholders or {},
    )


def clear_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the per-entry connection issues after a successful fetch."""
    for key in ENTRY_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry_id))


def create_metadata_issue(hass: HomeAssistant, statistic_id: str, fields: str) -> None:
    """Existing statistic metadata differs; imports for it are paused."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_METADATA_MISMATCH, statistic_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_METADATA_MISMATCH,
        translation_placeholders={"statistic_id": statistic_id, "fields": fields},
    )


def clear_metadata_issue(hass: HomeAssistant, statistic_id: str) -> None:
    """Clear a metadata issue once the metadata matches again."""
    ir.async_delete_issue(
        hass, DOMAIN, _issue_id(ISSUE_METADATA_MISMATCH, statistic_id)
    )


def create_new_meter_issue(hass: HomeAssistant, statistic_id: str) -> None:
    """A new meter appeared alongside existing history (e.g. a replacement)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_NEW_METER, statistic_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NEW_METER,
        translation_placeholders={"statistic_id": statistic_id},
    )
