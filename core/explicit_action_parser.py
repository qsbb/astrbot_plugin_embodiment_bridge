from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


ActionName = Literal[
    "dance",
    "dance_next",
    "raise_hand",
    "turn_half",
    "wave",
    "bow",
    "sit",
    "lie",
]
ParseStatus = Literal["matched", "rejected", "ambiguous", "not_explicit"]

MAX_COMMAND_CHARS = 96


@dataclass(frozen=True, slots=True)
class ExplicitActionResult:
    action: ActionName | None
    status: ParseStatus
    reason: str
    allow_model_tool: bool


_ZH_PREFIX = (
    r"(?:(?:现在|立刻|马上)\s*)?"
    r"(?:(?:请|请你|麻烦|麻烦你|给我|请给我|随便|请随便|请你随便|能不能|能否|可不可以)\s*)?"
)
_ZH_SUFFIX = r"\s*(?:(?:一下|一下吧|吧|吗|嘛|可以吗|好不好))?[。！!？?]?"
_EN_PREFIX = r"(?:(?:please|now|please\s+now|now\s+please)\s+)?"
_EN_SUFFIX = r"(?:\s+please)?[.!]?"

_ACTION_PATTERNS: dict[ActionName, tuple[str, ...]] = {
    "dance": (
        rf"{_ZH_PREFIX}(?:跳舞|跳个舞|跳一支舞|跳支舞){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:dance|do\s+a\s+dance|dance\s+for\s+me){_EN_SUFFIX}",
    ),
    "dance_next": (
        rf"{_ZH_PREFIX}(?:下一支舞|跳下一支舞|换一支舞|换支舞|换个舞蹈|换一个舞蹈|换个舞|换一个舞){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:next\s+dance|do\s+the\s+next\s+dance|switch\s+to\s+the\s+next\s+dance){_EN_SUFFIX}",
    ),
    "raise_hand": (
        rf"{_ZH_PREFIX}(?:举手|举起手|把手举起来|抬起手){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:raise\s+your\s+hand|put\s+your\s+hand\s+up){_EN_SUFFIX}",
    ),
    "turn_half": (
        rf"{_ZH_PREFIX}(?:转半圈|旋转半圈){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:turn\s+halfway|turn\s+half\s+way|turn\s+180\s+degrees|make\s+a\s+half\s+turn){_EN_SUFFIX}",
    ),
    "wave": (
        rf"{_ZH_PREFIX}(?:挥手|挥挥手|向我挥手){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:wave|wave\s+at\s+me){_EN_SUFFIX}",
    ),
    "bow": (
        rf"{_ZH_PREFIX}(?:鞠躬|鞠个躬){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:bow|take\s+a\s+bow){_EN_SUFFIX}",
    ),
    "sit": (
        rf"{_ZH_PREFIX}(?:坐下|请坐){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:sit|sit\s+down){_EN_SUFFIX}",
    ),
    "lie": (
        rf"{_ZH_PREFIX}(?:躺下){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:lie\s+down){_EN_SUFFIX}",
    ),
}

_COMPILED_ACTIONS = {
    action: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for action, patterns in _ACTION_PATTERNS.items()
}

_MENTION_PATTERNS: dict[ActionName, re.Pattern[str]] = {
    "dance_next": re.compile(
        r"跳下一支舞|下一支舞|换一支舞|换支舞|换个舞蹈|换一个舞蹈|换个舞|换一个舞|\bnext\s+dance\b|"
        r"\bswitch\s+to\s+the\s+next\s+dance\b",
        re.IGNORECASE,
    ),
    "dance": re.compile(r"跳舞|跳个舞|跳一支舞|跳支舞|\bdance\b", re.IGNORECASE),
    "raise_hand": re.compile(
        r"举手|举起手|把手举起来|抬起手|\braise\s+your\s+hand\b|"
        r"\bput\s+your\s+hand\s+up\b",
        re.IGNORECASE,
    ),
    "turn_half": re.compile(
        r"转半圈|旋转半圈|\bturn\s+half(?:way|\s+way)\b|"
        r"\bturn\s+180\s+degrees\b|\bhalf\s+turn\b",
        re.IGNORECASE,
    ),
    "wave": re.compile(r"挥手|挥挥手|\bwave\b", re.IGNORECASE),
    "bow": re.compile(r"鞠躬|鞠个躬|\bbow\b", re.IGNORECASE),
    "sit": re.compile(r"坐下|请坐|\bsit(?:\s+down)?\b", re.IGNORECASE),
    "lie": re.compile(r"躺下|\blie\s+down\b", re.IGNORECASE),
}

_QUOTED_RE = re.compile(r"[\"“”‘’「」『』《》〈〉]|(?<![A-Za-z])'|'(?![A-Za-z])")
_NEGATED_RE = re.compile(
    r"(?:不要|别|不许|禁止|不用|无需|不必|莫)\s*"
    r"|\b(?:do\s+not|don't|dont|never|must\s+not|should\s+not)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL_RE = re.compile(
    r"(?:如果|假如|假设|假若|要是|倘若|万一)"
    r"|\b(?:if|suppose|assuming|hypothetically|would|could)\b",
    re.IGNORECASE,
)
_REPORTED_RE = re.compile(
    r"(?:他说|她说|有人说|听说|据说|转述|复述|提到|表示|刚才说|命令我|要求我)"
    r"|\b(?:said|told\s+me|quoted|reported|mentioned)\b",
    re.IGNORECASE,
)
_DISCUSSION_RE = re.compile(
    r"(?:讨论|聊聊|分析|解释|是什么意思|怎么|如何|为什么|是否|会不会|"
    r"你会|想不想|喜欢|关于|作为例子|例如)"
    r"|\b(?:discuss|explain|what\s+does|how\s+to|why|whether|about|example)\b",
    re.IGNORECASE,
)


def parse_explicit_action(text: str) -> ExplicitActionResult:
    """Recognize only bounded, whole-message avatar action imperatives."""

    normalized = _normalize(text)
    if not normalized:
        return ExplicitActionResult(None, "not_explicit", "empty_input", True)
    if len(normalized) > MAX_COMMAND_CHARS:
        return ExplicitActionResult(None, "rejected", "input_too_long", False)

    mentions = _action_mentions(normalized)
    rejection = _rejection_reason(normalized, has_action_mention=bool(mentions))
    if rejection:
        return ExplicitActionResult(None, "rejected", rejection, False)

    matches = [
        action
        for action, patterns in _COMPILED_ACTIONS.items()
        if any(pattern.fullmatch(normalized) for pattern in patterns)
    ]
    if len(matches) == 1:
        return ExplicitActionResult(matches[0], "matched", "explicit_imperative", False)
    if len(matches) > 1:
        return ExplicitActionResult(None, "ambiguous", "multiple_actions", True)

    if len(mentions) > 1:
        return ExplicitActionResult(None, "ambiguous", "multiple_actions", True)
    if mentions:
        return ExplicitActionResult(None, "not_explicit", "not_full_match", True)
    return ExplicitActionResult(None, "not_explicit", "no_action_mentioned", True)


def _normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).strip().split())


def _rejection_reason(text: str, *, has_action_mention: bool) -> str:
    for pattern, reason in (
        (_QUOTED_RE, "quoted_or_cited"),
        (_NEGATED_RE, "negated"),
        (_HYPOTHETICAL_RE, "hypothetical"),
        (_REPORTED_RE, "reported_speech"),
    ):
        if pattern.search(text):
            return reason
    if has_action_mention and _DISCUSSION_RE.search(text):
        return "discussion_context"
    return ""


def _action_mentions(text: str) -> set[ActionName]:
    candidates: list[tuple[int, int, ActionName]] = []
    for action, pattern in _MENTION_PATTERNS.items():
        candidates.extend(
            (match.start(), match.end(), action) for match in pattern.finditer(text)
        )

    accepted: list[tuple[int, int, ActionName]] = []
    for candidate in sorted(
        candidates, key=lambda item: item[1] - item[0], reverse=True
    ):
        start, end, _action = candidate
        if any(
            outer_start <= start and end <= outer_end
            for outer_start, outer_end, _ in accepted
        ):
            continue
        accepted.append(candidate)
    return {action for _start, _end, action in accepted}
