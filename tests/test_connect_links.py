"""Connect links must come only from COMPOSIO_MANAGE_CONNECTIONS sign-in URLs, never from user data."""

import json

from apollo.composio_tools import ComposioTools

connect_links = ComposioTools.connect_links


def manage_result(results: dict) -> str:
    return json.dumps({"data": {"message": "…", "results": results}, "error": None, "successful": True})


def test_manage_connections_link_is_extracted():
    result = manage_result({
        "notion": {
            "toolkit": "notion",
            "status": "initiated",
            "redirect_url": "https://connect.composio.dev/link/lk_example",
            "instruction": "Render as clickable link: [Connect notion](https://connect.composio.dev/link/lk_example)",
        }
    })
    assert connect_links(result) == [("notion", "https://connect.composio.dev/link/lk_example")]


def test_already_connected_toolkit_yields_no_link():
    result = manage_result({"gmail": {"toolkit": "gmail", "status": "active"}})
    assert connect_links(result) == []


def test_links_inside_user_data_are_ignored():
    # Shape of a COMPOSIO_SEARCH_TOOLS result that included calendar events with links.
    result = json.dumps({"data": {"results": [{"events": [
        {"htmlLink": "https://www.google.com/calendar/event?eid=abc",
         "description": "Book here: https://outlook.office365.com/owa/calendar/bookings"}]}]}})
    assert connect_links(result) == []


def test_non_composio_redirect_url_is_rejected():
    result = manage_result({"slack": {"toolkit": "slack", "redirect_url": "https://evil.example/phish"}})
    assert connect_links(result) == []
    lookalike = manage_result({"slack": {"toolkit": "slack", "redirect_url": "https://composio.dev.evil.example/x"}})
    assert connect_links(lookalike) == []
    http = manage_result({"slack": {"toolkit": "slack", "redirect_url": "http://connect.composio.dev/link/x"}})
    assert connect_links(http) == []


def test_malformed_results_do_not_crash():
    assert connect_links("not json") == []
    assert connect_links(json.dumps(["a", "b"])) == []
    assert connect_links(json.dumps({"data": None})) == []
