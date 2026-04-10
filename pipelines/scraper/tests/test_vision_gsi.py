"""Tests for the Vision Government Solutions wrapper.

Tests extraction logic against offline HTML fixtures simulating the
VGSI ASP.NET GridView search results.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pipelines.scraper.models import (
    AuthConfig,
    FieldSpec,
    NavigationConfig,
    OutputContract,
    PaginationStrategy,
    RateLimitConfig,
    SelectorConfig,
    SiteProfile,
)
from pipelines.scraper.wrappers.vision_gsi import VisionGSIWrapper, build_vgsi_profile

VGSI_SEARCH_RESULTS_HTML = """
<html>
<body>
<form id="form1" method="post" action="./Search.aspx">
<input type="hidden" name="__VIEWSTATE" value="abc123" />
<input type="hidden" name="__EVENTVALIDATION" value="def456" />
<div id="MainContent_grdSearchResults">
<table class="GridStyle">
  <tr>
    <th>Owner</th>
    <th>Location</th>
    <th>Mblu</th>
    <th>Total Value</th>
  </tr>
  <tr>
    <td><a href="Parcel.aspx?pid=1001">SMITH JOHN</a></td>
    <td>123 MAIN ST</td>
    <td>001-002-003</td>
    <td>$250,000</td>
  </tr>
  <tr>
    <td><a href="Parcel.aspx?pid=1002">DOE JANE</a></td>
    <td>456 ELM ST</td>
    <td>004-005-006</td>
    <td>$175,000</td>
  </tr>
  <tr>
    <td><a href="Parcel.aspx?pid=1003">JOHNSON ROBERT</a></td>
    <td>789 OAK AVE</td>
    <td>007-008-009</td>
    <td>$320,000</td>
  </tr>
  <tr>
    <td colspan="4">
      <a href="javascript:__doPostBack('ctl00$MainContent$grdSearchResults','Page$2')">2</a>
      <a href="javascript:__doPostBack('ctl00$MainContent$grdSearchResults','Page$3')">3</a>
    </td>
  </tr>
</table>
</div>
</form>
</body>
</html>
"""

VGSI_EMPTY_RESULTS_HTML = """
<html>
<body>
<form id="form1" method="post">
<input type="hidden" name="__VIEWSTATE" value="xyz" />
<div>No records found</div>
</form>
</body>
</html>
"""


def _make_vgsi_profile() -> SiteProfile:
    return SiteProfile(
        site_id="vision_gsi_woonsocketri",
        display_name="VGSI - Woonsocket, RI",
        base_url="https://gis.vgsi.com/WoonsocketRI",
        auth=AuthConfig(type="none"),
        rate_limit=RateLimitConfig(min_delay_s=0.01, max_delay_s=0.02),
        requires_browser=True,
        navigation=NavigationConfig(
            search_url_template="https://gis.vgsi.com/WoonsocketRI/Search.aspx",
            pagination=PaginationStrategy.ASPNET_POSTBACK,
            results_container_selector="table.GridStyle",
        ),
        selectors=SelectorConfig(
            result_item="table.GridStyle tr",
            field_map={
                "owner": "owner",
                "address": "location",
                "mblu": "mblu",
                "assessment": "total_value",
            },
        ),
        output_contract=OutputContract(
            fields=[
                FieldSpec(name="owner", type="str", required=True),
                FieldSpec(name="address", type="str", required=True),
                FieldSpec(name="mblu", type="str", required=False),
                FieldSpec(name="assessment", type="str", required=False),
                FieldSpec(name="detail_url", type="url", required=False),
            ],
            dedup_fields=["owner", "address"],
            min_records_per_search=1,
        ),
        max_pages_per_search=10,
    )


class TestVisionGSIExtraction:
    def _make_wrapper(self) -> VisionGSIWrapper:
        profile = _make_vgsi_profile()
        wrapper = VisionGSIWrapper.__new__(VisionGSIWrapper)
        wrapper._profile = profile
        wrapper._errors = []
        wrapper._warnings = []
        wrapper._viewstate_cache = {}
        return wrapper

    def test_extracts_property_records(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        assert len(records) == 3

    def test_extracts_owner_names(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        assert records[0]["owner"] == "SMITH JOHN"
        assert records[1]["owner"] == "DOE JANE"
        assert records[2]["owner"] == "JOHNSON ROBERT"

    def test_extracts_addresses(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        assert records[0]["address"] == "123 MAIN ST"

    def test_extracts_detail_urls(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        assert "Parcel.aspx?pid=1001" in records[0].get("detail_url", "")

    def test_extracts_assessment_values(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        assert records[0]["assessment"] == "$250,000"

    def test_skips_pagination_row(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_SEARCH_RESULTS_HTML)
        for rec in records:
            assert "Page$" not in str(rec)

    def test_empty_results(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(VGSI_EMPTY_RESULTS_HTML)
        assert records == []

    @pytest.mark.asyncio
    async def test_postback_uses_parameterized_evaluate(self) -> None:
        wrapper = self._make_wrapper()
        page = AsyncMock()
        page.content = AsyncMock(
            side_effect=[
                VGSI_SEARCH_RESULTS_HTML,
                VGSI_SEARCH_RESULTS_HTML,
            ]
        )
        page.evaluate = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        wrapper._actions = AsyncMock()
        wrapper._actions.page = page
        wrapper._behavior = AsyncMock()
        wrapper._behavior.between_navigations_pause = AsyncMock()
        wrapper._errors = []

        ok = await wrapper._do_paginate(current_page=1)

        assert ok
        page.evaluate.assert_awaited()
        call_args = page.evaluate.await_args.args
        assert "([target, argument]) => __doPostBack(target, argument)" in call_args[0]


class TestBuildVGSIProfile:
    def test_generates_valid_profile_dict(self) -> None:
        profile_dict = build_vgsi_profile("WoonsocketRI", display_name="Woonsocket, RI")
        assert profile_dict["site_id"] == "vision_gsi_woonsocketri"
        assert "gis.vgsi.com/WoonsocketRI" in profile_dict["base_url"]
        assert profile_dict["requires_browser"] is True

    def test_rate_limits_are_conservative(self) -> None:
        profile_dict = build_vgsi_profile("TestTown")
        rl = profile_dict["rate_limit"]
        assert rl["min_delay_s"] >= 5.0
        assert rl["long_pause_min_s"] >= 45.0
