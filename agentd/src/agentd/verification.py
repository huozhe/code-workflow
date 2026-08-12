"""§10.1 verification block — render / upsert between sentinels (#50 / M5-0).

The gateway writes this block on feature_merged → AWAITING_VERIFICATION so
classification at issues.closed has something to read. Agents may refine steps
between the sentinels only; prose outside is never touched.
"""

from __future__ import annotations

import re
from typing import Sequence

SENTINEL_OPEN = "<!-- agentd:verification v1 -->"
SENTINEL_CLOSE = "<!-- /agentd:verification -->"

# Line that classifies VERIFIED vs ABANDONED at issues.closed (§10.3).
CHECKBOX_UNCHECKED = "- [ ] Human Verification Complete"
CHECKBOX_CHECKED = "- [x] Human Verification Complete"

ORDERING_LINE = (
    "*Tick this box before closing the issue — closing with it unticked "
    "records the session as `ABANDONED` (§10.3).*"
)

# Match either checkbox form (case-insensitive x; flexible space).
_CHECKBOX_RE = re.compile(
    r"^-\s+\[([ xX])\]\s+Human Verification Complete\s*$",
    re.MULTILINE,
)

_BLOCK_RE = re.compile(
    re.escape(SENTINEL_OPEN) + r".*?" + re.escape(SENTINEL_CLOSE),
    re.DOTALL,
)


def format_merged_prs(pr_nums: Sequence[int | str]) -> str:
    """Stable Merged PRs line from design/feature (and any extra) numbers."""
    seen: list[str] = []
    for n in pr_nums:
        if n is None or n == "":
            continue
        try:
            s = f"#{int(n)}"
        except (TypeError, ValueError):
            s = str(n)
            if not s.startswith("#"):
                s = f"#{s}"
        if s not in seen:
            seen.append(s)
    if not seen:
        return "**Merged PRs:** _(none recorded)_"
    return "**Merged PRs:** " + ", ".join(seen)


def format_not_covered(not_covered: str | Sequence[str] | None) -> str:
    if not_covered is None:
        return "**Not covered:** _(none listed — Architect may refine)_"
    if isinstance(not_covered, str):
        text = not_covered.strip()
        if not text:
            return "**Not covered:** _(none listed — Architect may refine)_"
        if text.lower().startswith("**not covered:**"):
            return text
        return f"**Not covered:** {text}"
    lines = [str(x).strip() for x in not_covered if str(x).strip()]
    if not lines:
        return "**Not covered:** _(none listed — Architect may refine)_"
    if len(lines) == 1:
        return f"**Not covered:** {lines[0]}"
    body = "\n".join(f"- {ln}" for ln in lines)
    return f"**Not covered:**\n{body}"


def format_steps(steps: Sequence[str] | None) -> str:
    if not steps:
        return (
            "1. `git pull origin main`\n"
            "2. Run the project's tests and smoke the merged change "
            "(see Feature PR).\n"
            "3. Confirm the session issue goal is met."
        )
    out: list[str] = []
    for i, step in enumerate(steps, start=1):
        s = str(step).strip()
        if not s:
            continue
        # Strip a leading "N." if the caller already numbered.
        s = re.sub(r"^\d+\.\s*", "", s)
        out.append(f"{i}. {s}")
    if not out:
        return format_steps(None)
    return "\n".join(out)


def render_verification_block(
    *,
    steps: Sequence[str] | None = None,
    merged_prs: Sequence[int | str] | None = None,
    not_covered: str | Sequence[str] | None = None,
    checked: bool = False,
) -> str:
    """Full sentinel-delimited §10.1 block (no leading/trailing blank lines)."""
    box = CHECKBOX_CHECKED if checked else CHECKBOX_UNCHECKED
    parts = [
        SENTINEL_OPEN,
        "## Verification Protocol",
        "",
        format_steps(steps),
        "",
        format_merged_prs(merged_prs or ()),
        format_not_covered(not_covered),
        "",
        box,
        "",
        ORDERING_LINE,
        SENTINEL_CLOSE,
    ]
    return "\n".join(parts)


def extract_verification_block(body: str) -> str | None:
    """Return the full sentinel-delimited block, or None if absent."""
    m = _BLOCK_RE.search(body or "")
    return m.group(0) if m else None


def checkbox_is_checked(
    block_or_body: str, *, strict: bool = False
) -> bool | None:
    """True/False from Human Verification line; None if absent.

    By default (``strict=False``) falls back to a bare-line search when the
    sentinel block is missing — useful for unit tests and restore probes.

    Callers that classify a session (M5-2/3, §10.3) **must** pass
    ``strict=True`` so only the in-sentinel line counts; a bare
    ``- [x] Human Verification Complete`` outside the block is not verified.
    """
    block = extract_verification_block(block_or_body)
    if block is None:
        if strict:
            return None
        # Bare fragment fallback (tests / restore detection of B5 bare ticks).
        text = block_or_body or ""
        if SENTINEL_OPEN not in text and CHECKBOX_UNCHECKED[:10] not in text:
            if not _CHECKBOX_RE.search(text):
                return None
        m = _CHECKBOX_RE.search(text)
    else:
        m = _CHECKBOX_RE.search(block)
    if not m:
        return None
    return m.group(1).lower() == "x"


def neutralize_bare_verification_ticks(body: str) -> str:
    """Uncheck bare Human Verification lines when no sentinel block is present.

    B5: an agent can add ``- [x] Human Verification Complete`` outside the
    protocol block; with no sentinels, ``set_checkbox_in_body`` is a no-op.
    """
    body = body or ""
    if extract_verification_block(body) is not None:
        return body
    if not _CHECKBOX_RE.search(body):
        return body
    return _CHECKBOX_RE.sub(CHECKBOX_UNCHECKED, body)


def set_checkbox_in_body(body: str, *, checked: bool) -> str:
    """Force the Human Verification line inside sentinels; no-op if block absent.

    Used to restore the checkbox after an agent edit that changed it (§10.2).
    Steps / Not covered / prose outside sentinels are left alone.
    """
    body = body or ""
    block = extract_verification_block(body)
    if block is None:
        return body
    line = CHECKBOX_CHECKED if checked else CHECKBOX_UNCHECKED
    if _CHECKBOX_RE.search(block):
        new_block = _CHECKBOX_RE.sub(line, block, count=1)
    else:
        # Block exists but line missing — insert before close sentinel.
        new_block = block.replace(
            SENTINEL_CLOSE,
            f"{line}\n\n{ORDERING_LINE}\n{SENTINEL_CLOSE}"
            if ORDERING_LINE not in block
            else f"{line}\n{SENTINEL_CLOSE}",
        )
    # replace(block, …) keeps the match that extract found — safer than
    # independent .index() of OPEN/CLOSE (stray sentinels in prose).
    return body.replace(block, new_block, 1)


def reinsert_verification_block(body: str, block: str) -> str:
    """Append a verification block when the body no longer has one.

    Keeps agent prose outside the sentinels; used when an agent deletes the
    whole §10.1 block (strictly worse than unticking — §10.3 would ABANDON).
    """
    body = body or ""
    if not block or extract_verification_block(body) is not None:
        return body
    if body.strip():
        if body.endswith("\n\n"):
            sep = ""
        elif body.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        out = body + sep + block
    else:
        out = block
    return out if out.endswith("\n") else out + "\n"


def default_steps_for_session(
    *,
    design_pr: int | str | None,
    feature_pr: int | str | None,
) -> list[str]:
    """Gateway scaffold steps — Architect may replace with human-runnable detail."""
    prs: list[str] = []
    for n in (design_pr, feature_pr):
        if n is None or n == "":
            continue
        try:
            prs.append(f"#{int(n)}")
        except (TypeError, ValueError):
            prs.append(f"#{n}" if not str(n).startswith("#") else str(n))
    pr_phrase = ", ".join(prs) if prs else "the merged Design/Feature PRs"
    return [
        f"`git pull origin main` — confirm {pr_phrase} are on `main`.",
        "Run the project's test suite / smoke checks for the merged change.",
        "Confirm the session issue goal is met in a running environment.",
    ]


def upsert_verification_block(
    body: str,
    *,
    steps: Sequence[str] | None = None,
    merged_prs: Sequence[int | str] | None = None,
    not_covered: str | Sequence[str] | None = None,
    checked: bool | None = None,
    preserve_checkbox: bool = True,
) -> str:
    """Replace the sentinel block or append it. Never edit prose outside.

    If *preserve_checkbox* and the body already has a checked box, keep it
    unless *checked* is explicitly set.
    """
    body = body or ""
    existing = extract_verification_block(body)
    if checked is None:
        if preserve_checkbox and existing is not None:
            prev = checkbox_is_checked(existing)
            checked = bool(prev)
        else:
            checked = False

    new_block = render_verification_block(
        steps=steps,
        merged_prs=merged_prs,
        not_covered=not_covered,
        checked=checked,
    )

    if existing is not None:
        return body[: body.index(SENTINEL_OPEN)] + new_block + body[
            body.index(SENTINEL_CLOSE) + len(SENTINEL_CLOSE) :
        ]

    # Append with a separating blank line when body is non-empty.
    if body.strip():
        sep = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
        return body + sep + new_block + "\n"
    return new_block + "\n"
