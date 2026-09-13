"""Google Trends connector — search interest for BRILink-related terms.

``pytrends`` wraps the public Google Trends internal API (no official API key
needed).  The connector fetches interest-over-time and interest-by-region for
BRILink-related search terms, then emits each data point as a ``Document``
with the trend metrics in ``engagement``.

Use cases
---------
* Spike detection: sudden search interest often correlates with news events.
* Regional demand signal: provinces with high interest may need more agents.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from ..logging_config import get_logger
from .base import BaseConnector, ConnectorUnavailable, Document

logger = get_logger(__name__)

# Search terms to track; pytrends allows up to 5 at once.
TREND_TERMS = [
    "agen BRILink",
    "BRILink",
    "laku pandai",
    "BRILink Mobile",
    "agen BRI",
]

GEO = "ID"


class GoogleTrendsConnector(BaseConnector):
    name = "google_trends"
    platform = "google_trends"
    description = "Google Trends search interest for BRILink-related terms in Indonesia"
    default_enabled = True

    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        try:
            from pytrends.request import TrendReq
        except ImportError as exc:  # pragma: no cover
            raise ConnectorUnavailable(f"pytrends not installed: {exc}") from exc

        try:
            pytrends = TrendReq(hl="id-ID", tz=480, retries=3, backoff_factor=1.0)
        except Exception as exc:
            logger.warning("Failed to initialise pytrends: %s", exc)
            return

        emitted = 0
        emitted = yield from self._fetch_interest_over_time(pytrends, since, limit, emitted)
        if emitted < limit:
            yield from self._fetch_interest_by_region(pytrends, since, limit, emitted)

    def _fetch_interest_over_time(self, pytrends, since, limit, emitted):
        try:
            pytrends.build_payload(TREND_TERMS[:5], cat=0, timeframe="today 3-m", geo=GEO)
            df = pytrends.interest_over_time()
        except Exception as exc:
            logger.warning("Google Trends interest_over_time failed: %s", exc)
            return emitted

        if df is None or df.empty:
            return emitted

        for col in df.columns:
            if col == "isPartial":
                continue
            for ts, val in df[col].items():
                if int(val) == 0:
                    continue
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < since:
                    continue
                yield Document(
                    source_platform=self.platform,
                    source_name="interest_over_time",
                    external_id=f"trends_iot_{col}_{ts.strftime('%Y%m%d')}",
                    title=f"Search interest: {col}",
                    body=f"Google Trends score {int(val)} for '{col}' in Indonesia on {ts.strftime('%Y-%m-%d')}.",
                    url=f"https://trends.google.com/trends/explore?date=today%203-m&geo=ID&q={col.replace(' ', '+')}",
                    published_at=dt,
                    engagement={"trend_score": int(val), "term": col},
                    raw_payload={
                        "collector": "pytrends",
                        "metric": "interest_over_time",
                        "geo": GEO,
                    },
                )
                emitted += 1
                if emitted >= limit:
                    return emitted
        return emitted

    def _fetch_interest_by_region(self, pytrends, since, limit, emitted):
        try:
            pytrends.build_payload(TREND_TERMS[:5], cat=0, timeframe="today 3-m", geo=GEO)
            df = pytrends.interest_by_region(
                resolution="PROVINCE", inc_low_vol=True, inc_geo_code=True
            )
        except Exception as exc:
            logger.warning("Google Trends interest_by_region failed: %s", exc)
            return emitted

        if df is None or df.empty:
            return emitted

        dt_now = datetime.now(timezone.utc)
        for col in df.columns:
            if col == "geoCode":
                continue
            for region_name, row in df[col].items():
                val = int(row)
                if val == 0:
                    continue
                geo_code = str(df.loc[region_name].get("geoCode", ""))
                yield Document(
                    source_platform=self.platform,
                    source_name="interest_by_region",
                    external_id=f"trends_region_{col}_{geo_code}_{dt_now.strftime('%Y%m%d')}",
                    title=f"Regional interest: {col} in {region_name}",
                    body=f"Google Trends regional score {val} for '{col}' in {region_name} (Indonesia).",
                    url=f"https://trends.google.com/trends/explore?date=today%203-m&geo=ID&q={col.replace(' ', '+')}",
                    published_at=dt_now,
                    engagement={"trend_score": val, "term": col, "region": region_name},
                    raw_payload={
                        "collector": "pytrends",
                        "metric": "interest_by_region",
                        "geo": GEO,
                        "geo_code": geo_code,
                    },
                )
                emitted += 1
                if emitted >= limit:
                    return emitted
        return emitted
