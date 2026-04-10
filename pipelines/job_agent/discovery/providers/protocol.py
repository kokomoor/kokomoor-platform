"""ProviderAdapter -- structural contract for all job board adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from typing import ClassVar

    from playwright.async_api import Page

    from pipelines.job_agent.discovery.captcha import CaptchaHandler
    from pipelines.job_agent.discovery.human_behavior import HumanBehavior
    from pipelines.job_agent.discovery.models import DiscoveryConfig, ListingRef
    from pipelines.job_agent.discovery.rate_limiter import DomainRateLimiter
    from pipelines.job_agent.models import JobSource, SearchCriteria


@runtime_checkable
class ProviderAdapter(Protocol):
    """Structural typing contract for all provider adapters.

    Browser providers extend ``BaseProvider`` which satisfies this protocol.
    HTTP providers (Greenhouse, Lever) implement it directly.
    """

    source: ClassVar[JobSource]

    def requires_auth(self) -> bool: ...

    def base_domain(self) -> str:
        """Primary domain for this provider (used for pre-navigation warm-up)."""
        ...

    async def is_authenticated(self, page: Page) -> bool:
        """Check whether the current page/session is authenticated."""
        ...

    async def authenticate(
        self,
        page: Page,
        *,
        email: str,
        password: str,
        behavior: HumanBehavior,
    ) -> bool:
        """Perform the login flow. Return True if successful."""
        ...

    async def run_search(
        self,
        page: Page,
        criteria: SearchCriteria,
        config: DiscoveryConfig,
        *,
        behavior: HumanBehavior,
        rate_limiter: DomainRateLimiter,
        captcha_handler: CaptchaHandler,
    ) -> list[ListingRef]:
        """Execute search and return discovered listing refs."""
        ...
