"""Parse common lumber size/length tokens from freeform descriptions."""

import re
from typing import Optional, Tuple

SIZE_PATTERNS = [
    re.compile(r"\b(\d+\s*/\s*\d+)\s*[xX]\s*(\d+)\b"),
    re.compile(r"\b(\d+)\s*[xX]\s*(\d+)\b"),
]
LENGTH_PATTERN = re.compile(r"\b(\d{1,2})(?:\s*(?:ft|foot|feet|'))\b")
DIMENSION_TRIPLE = re.compile(r"\b(\d+(?:\s*/\s*\d+)?)\s*[xX]\s*(\d+)\s*[xX]\s*(\d{1,2})\b")


def parse_size_and_length(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract likely size + length from text like '2x10 16 treated' or '5/4x6x16'."""
    if not text:
        return None, None

    lowered = text.lower()

    triple = DIMENSION_TRIPLE.search(lowered)
    if triple:
        size = f"{triple.group(1).replace(' ', '')}x{triple.group(2)}"
        length = triple.group(3)
        return size, length

    size = None
    for pattern in SIZE_PATTERNS:
        match = pattern.search(lowered)
        if match:
            left = match.group(1).replace(" ", "")
            size = f"{left}x{match.group(2)}"
            break

    length_match = LENGTH_PATTERN.search(lowered)
    length = length_match.group(1) if length_match else None

    if size and not length:
        numbers = re.findall(r"\b(\d{1,2})\b", lowered)
        size_numbers = set(re.findall(r"\d+", size))
        for number in numbers:
            if number not in size_numbers:
                length = number
                break

    return size, length
