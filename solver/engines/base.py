"""Engine interface. Every recognition backend implements solve()."""

from abc import ABC, abstractmethod

import numpy as np


class BaseEngine(ABC):
    name = "base"
    wants_binary = True  # False = engine consumes the RAW image itself

    @abstractmethod
    def solve(self, image: np.ndarray) -> str:
        """Take a preprocessed (black-text/white-bg) image, return the text."""
        raise NotImplementedError

    def available(self) -> bool:
        return True
