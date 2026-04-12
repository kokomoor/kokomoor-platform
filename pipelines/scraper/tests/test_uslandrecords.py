"""Tests for the US Land Records wrapper.

Tests extraction logic, vendor detection, and profile generation
against offline HTML fixtures.
"""

from __future__ import annotations

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
from pipelines.scraper.wrappers.uslandrecords import (
    USLandRecordsWrapper,
    VendorType,
    build_uslandrecords_profile,
)

LAREDO_RESULTS_HTML = """
<html>
<body>
<div class="laredoApp">
<table class="searchResults">
  <tr>
    <th>Doc Type</th>
    <th>Grantor</th>
    <th>Grantee</th>
    <th>Record Date</th>
    <th>Book/Page</th>
    <th>Consideration</th>
  </tr>
  <tr>
    <td>DEED</td>
    <td>SMITH JOHN</td>
    <td>DOE JANE</td>
    <td>01/15/2024</td>
    <td><a href="/doc/view?id=12345">1234/567</a></td>
    <td>$350,000</td>
  </tr>
  <tr>
    <td>MORTGAGE</td>
    <td>DOE JANE</td>
    <td>FIRST NATIONAL BANK</td>
    <td>01/15/2024</td>
    <td><a href="/doc/view?id=12346">1234/568</a></td>
    <td>$280,000</td>
  </tr>
  <tr>
    <td>LIEN</td>
    <td>CITY OF WOONSOCKET</td>
    <td>JOHNSON ROBERT</td>
    <td>02/01/2024</td>
    <td><a href="/doc/view?id=12347">1235/001</a></td>
    <td>$5,000</td>
  </tr>
</table>
</div>
</body>
</html>
"""

EMPTY_RESULTS_HTML = """
<html>
<body>
<div>No documents found matching your search criteria.</div>
</body>
</html>
"""

TERMS_PAGE_HTML = """
<html>
<body>
<h1>Terms of Use</h1>
<p>By clicking Accept, you agree to our terms.</p>
<input type="submit" id="btnAccept" value="I Accept" />
</body>
</html>
"""


def _make_uslr_profile() -> SiteProfile:
    return SiteProfile(
        site_id="uslandrecords_ri_woonsocket",
        display_name="US Land Records - Woonsocket, RI",
        base_url="https://i2l.uslandrecords.com/RI/Woonsocket",
        auth=AuthConfig(type="none"),
        rate_limit=RateLimitConfig(min_delay_s=0.01, max_delay_s=0.02),
        requires_browser=True,
        navigation=NavigationConfig(
            search_url_template="https://i2l.uslandrecords.com/RI/Woonsocket/searchentry.aspx",
            pagination=PaginationStrategy.NEXT_BUTTON,
            next_button_selector="a.nextPage",
        ),
        selectors=SelectorConfig(
            result_item="table.searchResults tr",
            field_map={
                "doc_type": "doc_type",
                "grantor": "grantor",
                "grantee": "grantee",
                "record_date": "record_date",
                "book_page": "bookpage",
                "consideration": "consideration",
            },
        ),
        output_contract=OutputContract(
            fields=[
                FieldSpec(name="doc_type", type="str", required=True),
                FieldSpec(name="grantor", type="str", required=True),
                FieldSpec(name="grantee", type="str", required=True),
                FieldSpec(name="record_date", type="date", required=False),
                FieldSpec(name="book_page", type="str", required=False),
                FieldSpec(name="consideration", type="str", required=False),
            ],
            dedup_fields=["grantor", "grantee", "record_date", "book_page"],
            min_records_per_search=1,
        ),
        max_pages_per_search=20,
    )


class TestUSLandRecordsExtraction:
    def _make_wrapper(self) -> USLandRecordsWrapper:
        profile = _make_uslr_profile()
        wrapper = USLandRecordsWrapper.__new__(USLandRecordsWrapper)
        wrapper._profile = profile
        wrapper._errors = []
        wrapper._warnings = []
        wrapper._vendor = VendorType.UNKNOWN
        wrapper._accepted_terms = False
        return wrapper

    def test_extracts_document_records(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(LAREDO_RESULTS_HTML)
        assert len(records) == 3

    def test_detects_laredo_vendor(self) -> None:
        wrapper = self._make_wrapper()
        wrapper.extract_from_fixture(LAREDO_RESULTS_HTML)
        assert wrapper._vendor == VendorType.LAREDO

    def test_extracts_doc_types(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(LAREDO_RESULTS_HTML)
        assert records[0]["doc_type"] == "DEED"
        assert records[1]["doc_type"] == "MORTGAGE"
        assert records[2]["doc_type"] == "LIEN"

    def test_extracts_parties(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(LAREDO_RESULTS_HTML)
        assert records[0]["grantor"] == "SMITH JOHN"
        assert records[0]["grantee"] == "DOE JANE"

    def test_extracts_doc_urls(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(LAREDO_RESULTS_HTML)
        assert any("doc_url" in r for r in records)

    def test_empty_results(self) -> None:
        wrapper = self._make_wrapper()
        records = wrapper.extract_from_fixture(EMPTY_RESULTS_HTML)
        assert records == []


class TestBuildUSLandRecordsProfile:
    def test_generates_valid_profile_dict(self) -> None:
        profile_dict = build_uslandrecords_profile("Woonsocket")
        assert profile_dict["site_id"] == "uslandrecords_ri_woonsocket"
        assert "Woonsocket" in profile_dict["base_url"]

    def test_conservative_rate_limits(self) -> None:
        profile_dict = build_uslandrecords_profile("Woonsocket")
        rl = profile_dict["rate_limit"]
        assert rl["min_delay_s"] >= 5.0
        assert rl["long_pause_min_s"] >= 60.0

    def test_custom_state(self) -> None:
        profile_dict = build_uslandrecords_profile("SomeTown", "MA")
        assert profile_dict["site_id"] == "uslandrecords_ma_sometown"
