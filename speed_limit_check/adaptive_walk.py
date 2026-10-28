"""Adaptive step sizing for the Street View segment walk - the pure logic
(no Street View/Vision calls of its own), so it can be unit-tested with
nothing but fake inputs.

The fixed-spacing walk (`find_bad_speed_limit.points_for_spacing`) pays for
a Street View image + OCR every N meters whether or not anything is there
to read. This module instead lets one cheap Gemini look at the
forward-facing frame at each stop decide how far to move next:

  - nothing sign-like in sight and the road ahead is clearly visible ->
    jump ahead by (most of) the distance Gemini says is clear, up to
    `max_step_m` (e.g. 100m on an open road);
  - a possible sign far ahead -> approach it, stopping `read_distance_m`
    short of it (close enough for a 640px frame to resolve the text, far
    enough that it's still well inside the field of view);
  - a sign already within reading range, or an obstructed/unclear view ->
    fall back to the fixed-walk's own small step (`min_step_m`) so nothing
    is skipped.

Gemini only ever chooses the NEXT POSITION. Every fetched image is still
OCR'd exactly as before, so detection of a sign in a frame we actually
looked at is unchanged - the risk this trades is a sign in a stretch
Gemini wrongly called clear, which `clear_safety` (only ever advance part
of the stated clear distance) and `min_step_m` (the floor whenever the
view is obstructed or Gemini fails) are there to limit.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from pydantic import BaseModel, Field


@dataclasses.dataclass(frozen=True)
class AdaptiveWalkConfig:
    min_step_m: float = 10.0       # smallest step - also the fallback whenever the view is unclear
    max_step_m: float = 100.0      # largest step, taken only when the road ahead is clearly empty
    read_distance_m: float = 20.0  # how far short of a distant sign to stop so it's readable but not missed
    clear_safety: float = 0.8      # advance at most this fraction of the stated clear distance


class LookAhead(BaseModel):
    """Gemini's structured answer for one forward-facing Street View frame."""
    sign_ahead: bool = Field(description="True if any roadside sign that could be a speed limit sign (white/black rectangular, "
                                          "or any sign too small/far to identify) is visible ahead, on either side of the road.")
    sign_distance_m: Optional[float] = Field(default=None, description="Estimated distance in meters from the camera to the nearest such sign, "
                                                                       "or null if sign_ahead is false.")
    clear_distance_m: float = Field(description="How many meters ahead along the road you can see clearly enough to be sure there is "
                                                "no sign-like object in it. Use a small value if the view is blocked by a curve, hill, "
                                                "large vehicle, trees, glare or an intersection. 0-150.")


LOOKAHEAD_SYSTEM_PROMPT = (
    "You are helping a survey vehicle decide how far to drive before taking its next street-level photo, to find "
    "US roadside speed limit signs. You see one forward-facing street-level photo. Estimate distances in meters "
    "using road width (about 3.5m per lane), lane markings, cars and poles as scale references. Be conservative: "
    "when unsure whether the road ahead is really clear, report a SHORT clear distance. Never report a clear "
    "distance beyond what you can actually see well, and treat anything blocked by a bend, crest, parked/large "
    "vehicle, dense foliage or an intersection as not clear."
)
LOOKAHEAD_USER_TEXT = (
    "Fill in: sign_ahead (any possible speed limit sign visible on either side of the road, even if too far to read), "
    "sign_distance_m (distance to the nearest one, else null), clear_distance_m (meters of road ahead you can see "
    "clearly with no sign-like object in it)."
)


def plan_advance(look: Optional[LookAhead], cfg: AdaptiveWalkConfig) -> tuple[float, str]:
    """Returns (meters to advance, short human-readable reason) for one
    observation. `look is None` (Gemini failed or was unavailable) is the
    safe fallback: the fixed walk's own minimum step."""
    if look is None:
        return cfg.min_step_m, "no look-ahead available - minimum step"

    if look.sign_ahead and look.sign_distance_m is not None and look.sign_distance_m > 0:
        d = look.sign_distance_m
        if d > cfg.read_distance_m:
            # Approach to read_distance_m short of it. At least min_step_m so we always make progress; at most
            # max_step_m, as the distance estimate is least reliable when far away - re-observe from closer.
            return min(max(d - cfg.read_distance_m, cfg.min_step_m), cfg.max_step_m), f"sign ~{d:.0f}m ahead - approaching to ~{cfg.read_distance_m:.0f}m"
        return cfg.min_step_m, f"sign ~{d:.0f}m ahead, already in reading range - passing it in minimum steps"

    if look.sign_ahead:
        return cfg.min_step_m, "sign-like object ahead with no distance estimate - minimum step"

    clear = max(0.0, min(look.clear_distance_m, cfg.max_step_m / cfg.clear_safety))
    step = clear * cfg.clear_safety
    return min(max(step, cfg.min_step_m), cfg.max_step_m), f"road clear for ~{look.clear_distance_m:.0f}m"


def next_position_m(current_m: float, advance_m: float, length_m: float, min_step_m: float) -> Optional[float]:
    """The next distance along the segment, or None once the walk is over.
    The segment's end itself is always visited once (the fixed walk also
    always includes its last point), unless we're already within half a
    minimum step of it."""
    if current_m >= length_m - 1e-6:
        return None
    nxt = current_m + max(advance_m, 0.1)
    if nxt >= length_m:
        return length_m if length_m - current_m > min_step_m / 2 else None
    return nxt
