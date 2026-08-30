from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import DiscoveredFile, ParsedFile, ProviderDescriptor, ProviderProbe


class ProviderAdapter(ABC):
    descriptor: ProviderDescriptor

    def __init__(self, user_home: Path):
        self.user_home = user_home

    def should_reparse(self, discovered: DiscoveredFile) -> bool:
        return False

    @abstractmethod
    def discover_files(self) -> list[DiscoveredFile]:
        raise NotImplementedError

    @abstractmethod
    def parse_file(self, discovered: DiscoveredFile) -> ParsedFile:
        raise NotImplementedError

    @abstractmethod
    def probe(self) -> ProviderProbe:
        raise NotImplementedError
