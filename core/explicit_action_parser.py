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
    "crouch",
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
    # Keep the subject/request wrappers bounded.  Quest voice turns commonly
    # phrase an imperative as "让她随便跳个舞" or "帮我跳个舞"; these are
    # still direct commands, while narrative/reported forms are rejected below.
    r"(?:(?:请|请你|麻烦|麻烦你|给我|请给我|能不能|能否|可不可以|"
    r"让(?:她|他|角色|它)?|叫(?:她|他|角色|它)?|帮我|请她|请他|请角色|"
    r"我让(?:她|他|角色|它)?|我想让(?:她|他|角色|它)?|你|她|他|角色|它|"
    r"开始|直接|来)\s*)?"
    r"(?:(?:随便|自然(?:地)?|轻轻(?:地)?)\s*)?"
)
_ZH_SUFFIX = r"\s*(?:(?:一下|一下吧|吧|吗|嘛|可以吗|好不好))?[。！!？?]?"
_EN_PREFIX = r"(?:(?:please|now|please\s+now|now\s+please)\s+)?"
_EN_SUFFIX = r"(?:\s+please)?[.!]?"

_ACTION_PATTERNS: dict[ActionName, tuple[str, ...]] = {
    "dance": (
        rf"{_ZH_PREFIX}(?:跳舞|跳个舞|跳一支舞|跳支舞|跳一段舞|跳一下舞|"
        rf"来个舞|来段舞|跳舞给我看|跳个舞给我看){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:dance|do\s+a\s+dance|dance\s+for\s+me){_EN_SUFFIX}",
    ),
    "dance_next": (
        rf"{_ZH_PREFIX}(?:下一支舞|播放下一支舞|跳下一支舞|换一支舞|换支舞|换个舞蹈|"
        rf"换一个舞蹈|换个舞|换一个舞|再来一个舞|再来一支舞|再跳一个|再跳一支){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:next\s+dance|do\s+the\s+next\s+dance|switch\s+to\s+the\s+next\s+dance){_EN_SUFFIX}",
    ),
    "raise_hand": (
        rf"{_ZH_PREFIX}(?:举手|举起手|把手举起来|把手抬起来|抬起手|抬手){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:raise\s+your\s+hand|put\s+your\s+hand\s+up){_EN_SUFFIX}",
    ),
    "turn_half": (
        rf"{_ZH_PREFIX}(?:转身|转个身|转过身|转半圈|旋转半圈){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:turn\s+around|turn\s+halfway|turn\s+half\s+way|turn\s+180\s+degrees|make\s+a\s+half\s+turn){_EN_SUFFIX}",
    ),
    "wave": (
        rf"{_ZH_PREFIX}(?:挥手|挥挥手|挥一下手|向我挥手|给我挥手){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:wave|wave\s+at\s+me){_EN_SUFFIX}",
    ),
    "bow": (
        rf"{_ZH_PREFIX}(?:鞠躬|鞠个躬|鞠一下躬){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:bow|take\s+a\s+bow){_EN_SUFFIX}",
    ),
    "sit": (
        rf"{_ZH_PREFIX}(?:坐下|请坐|坐一会儿){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:sit|sit\s+down){_EN_SUFFIX}",
    ),
    "lie": (
        rf"{_ZH_PREFIX}(?:躺下|躺一会儿){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:lie\s+down){_EN_SUFFIX}",
    ),
    "crouch": (
        rf"{_ZH_PREFIX}(?:下蹲|蹲下|蹲一下|蹲下来){_ZH_SUFFIX}",
        rf"{_EN_PREFIX}(?:crouch|squat|crouch\s+down|squat\s+down){_EN_SUFFIX}",
    ),
}

_COMPILED_ACTIONS = {
    action: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for action, patterns in _ACTION_PATTERNS.items()
}

_MENTION_PATTERNS: dict[ActionName, re.Pattern[str]] = {
    "dance_next": re.compile(
        r"跳下一支舞|下一支舞|播放下一支舞|换一支舞|换支舞|换个舞蹈|换一个舞蹈|换个舞|换一个舞|"
        r"再来一个舞|再来一支舞|再跳一个|再跳一支|\bnext\s+dance\b|"
        r"\bswitch\s+to\s+the\s+next\s+dance\b",
        re.IGNORECASE,
    ),
    "dance": re.compile(
        r"跳舞|跳个舞|跳一支舞|跳支舞|跳一段舞|跳一下舞|来个舞|来段舞|\bdance\b",
        re.IGNORECASE,
    ),
    "raise_hand": re.compile(
        r"举手|举起手|把手举起来|把手抬起来|抬起手|抬手|\braise\s+your\s+hand\b|"
        r"\bput\s+your\s+hand\s+up\b",
        re.IGNORECASE,
    ),
    "turn_half": re.compile(
        r"转身|转个身|转过身|转半圈|旋转半圈|\bturn\s+around\b|\bturn\s+half(?:way|\s+way)\b|"
        r"\bturn\s+180\s+degrees\b|\bhalf\s+turn\b",
        re.IGNORECASE,
    ),
    "wave": re.compile(r"挥手|挥挥手|挥一下手|向我挥手|给我挥手|\bwave\b", re.IGNORECASE),
    "bow": re.compile(r"鞠躬|鞠个躬|鞠一下躬|\bbow\b", re.IGNORECASE),
    "sit": re.compile(r"坐下|请坐|坐一会儿|\bsit(?:\s+down)?\b", re.IGNORECASE),
    "lie": re.compile(r"躺下|躺一会儿|\blie\s+down\b", re.IGNORECASE),
    "crouch": re.compile(
        r"下蹲|蹲下|蹲一下|蹲下来|\bcrouch(?:\s+down)?\b|\bsquat(?:\s+down)?\b",
        re.IGNORECASE,
    ),
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
_ZH_REPLY_SUFFIX_RE = re.compile(
    r"\s*(?=[,，、;；]|并|同时|也|还|顺便)[,，、;；]?\s*"
    r"(?:并(?:且)?\s*)?(?:(?:同时|也|还|顺便)\s*)?"
    r"(?:请\s*)?(?:(?:简短|简单)(?:地)?\s*)?"
    r"(?:回复|回答|回应|答复)(?:我(?:一下|一句|几句)?|一下|一句|几句|这个问题)?"
    r"[。！!？?]?\s*$",
    re.IGNORECASE,
)
_EN_REPLY_SUFFIX_RE = re.compile(
    r"\s*,?\s*(?:and|while)\s+(?:also\s+)?(?:briefly\s+)?"
    r"(?:reply|respond|answer)(?:\s+(?:to\s+)?me)?[.!?]?\s*$",
    re.IGNORECASE,
)
_DIRECT_REPLY_REQUEST_RE = re.compile(
    r"^(?:请\s*)?(?:(?:简短|简单)(?:地)?\s*)?"
    r"(?:回复|回答|回应|答复)(?:我(?:一下|一句|几句)?|一下|一句|几句|这个问题)[。！!？?]?$"
    r"|^(?:please\s+)?(?:briefly\s+)?(?:reply|respond|answer)"
    r"(?:\s+(?:to\s+)?me|\s+too|\s+as\s+well)[.!?]?$",
    re.IGNORECASE,
)
_NEGATED_REPLY_RE = re.compile(
    r"(?:不要|别|无需|不用|不必)\s*(?:(?:再|同时|也)\s*)?"
    r"(?:回复|回答|回应|答复)"
    r"|\b(?:do\s+not|don't|dont|never)\s+(?:reply|respond|answer)\b",
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

    action_text = _strip_required_reply_suffix(normalized)
    matches = [
        action
        for action, patterns in _COMPILED_ACTIONS.items()
        if any(pattern.fullmatch(action_text) for pattern in patterns)
    ]
    if len(matches) == 1:
        return ExplicitActionResult(
            matches[0],
            "matched",
            (
                "explicit_imperative_with_reply"
                if action_text != normalized
                else "explicit_imperative"
            ),
            False,
        )
    if len(matches) > 1:
        return ExplicitActionResult(None, "ambiguous", "multiple_actions", False)

    if len(mentions) > 1:
        return ExplicitActionResult(None, "ambiguous", "multiple_actions", False)
    if mentions:
        return ExplicitActionResult(None, "not_explicit", "not_full_match", True)
    return ExplicitActionResult(None, "not_explicit", "no_action_mentioned", True)


def _normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).strip().split())


def requires_text_reply(text: str) -> bool:
    """Recognize a bounded, explicit request for same-turn reply text."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > MAX_COMMAND_CHARS * 2:
        return False
    if _NEGATED_REPLY_RE.search(normalized):
        return False
    return bool(
        _ZH_REPLY_SUFFIX_RE.search(normalized)
        or _EN_REPLY_SUFFIX_RE.search(normalized)
        or _DIRECT_REPLY_REQUEST_RE.search(normalized)
    )


def _strip_required_reply_suffix(text: str) -> str:
    if not requires_text_reply(text):
        return text
    for pattern in (_ZH_REPLY_SUFFIX_RE, _EN_REPLY_SUFFIX_RE):
        match = pattern.search(text)
        if match is not None and match.start() > 0:
            return text[: match.start()].rstrip(" ,，、;；")
    return text


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
