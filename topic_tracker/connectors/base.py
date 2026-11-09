"""Every platform implements this; the analysis code never sees platform-specific data."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Dataset
from ..text import TopicQuery


class Connector(ABC):
    platform: str = ""

    @abstractmethod
    def collect(self, query: TopicQuery, dataset: Dataset) -> None:
        """Search the platform for `query` and add normalised Posts/Accounts to `dataset`."""

    def refresh(self, dataset: Dataset, now: datetime) -> None:
        """Re-fetch current engagement for posts of this platform already in `dataset`."""
        raise NotImplementedError
