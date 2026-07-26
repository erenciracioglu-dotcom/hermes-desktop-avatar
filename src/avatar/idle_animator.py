"""Idle animator — weighted random picker with rarity tiers.

The avatar's idle state has many sub-animations (sighs, dances, jumps,
etc.).  To avoid monotony without showing inappropriate animations too
often, each animation declares a *rarity* — higher rarity means rarer
picks.  We implement this as weighted random sampling where the weight
of an animation is ``1.0 / rarity``.

Rarity conventions (from Eren, 2026-07-18):
  - a-tier (rarity 1): most frequent — the bread-and-butter idle
    variations.  Stops the avatar from looking static.
  - b-tier (rarity 4): roughly 4x rarer than a-tier.  Shows up once
    every handful of a-tier loops.
  - c-tier (rarity 20): roughly 20x rarer than a-tier.  Reserved for
    "reward" animations that a watching user might not see for a
    while — gives attentive watchers something to look forward to.

The actual pool composition is supplied by the caller (typically parsed
from a video directory).  This module doesn't know about sprites, video
files, or the avatar — it just picks one of N items by rarity.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class IdleAnimation:
    """A single idle animation candidate.

    Attributes:
        id: stable identifier (e.g. "nora_idle_a_sigh0").
        rarity: positive number.  Weight = 1.0 / rarity.  Higher rarity
            means the animation is picked less often.
        label: optional human-readable label for logs and the settings UI.
    """
    id: str
    rarity: float = 1.0
    label: str = ""

    @property
    def weight(self) -> float:
        if self.rarity <= 0:
            raise ValueError(f"rarity must be > 0, got {self.rarity}")
        return 1.0 / self.rarity


@dataclass
class IdleAnimator:
    """Holds a pool of IdleAnimation objects and picks one on demand.

    Usage::

        animator = IdleAnimator([
            IdleAnimation("nora_a_sigh0", rarity=1),
            IdleAnimation("nora_a_sigh1", rarity=1),
            IdleAnimation("nora_b_jump",  rarity=4),
            IdleAnimation("nora_c_dance", rarity=20),
        ])
        picked = animator.pick_random()
    """
    pool: Sequence[IdleAnimation] = field(default_factory=tuple)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        # copy pool to a list so callers can mutate it; also reject
        # non-positive rarities early.
        self._pool: list[IdleAnimation] = list(self.pool)
        self._last_per_tier: dict[float, str] = {}
        for a in self._pool:
            if a.rarity <= 0:
                raise ValueError(
                    f"animation {a.id!r} has invalid rarity {a.rarity}"
                )

    # ------------------------------------------------------------------ mutators
    def register(self, animation: IdleAnimation) -> None:
        self._pool.append(animation)

    def set_pool(self, animations: Sequence[IdleAnimation]) -> None:
        self._pool = list(animations)

    @property
    def pool_size(self) -> int:
        return len(self._pool)

    def total_weight(self) -> float:
        return sum(a.weight for a in self._pool)

    # ------------------------------------------------------------------ picker
    def pick_random(self, exclude_recent: Sequence[str] = ()) -> IdleAnimation | None:
        """Weighted-random pick.  Returns None only if the pool is empty.

        ``exclude_recent`` is an optional list of animation ids that
        must NOT be picked.  If excluding those would empty the pool,
        the exclusion is ignored so a pick is always possible.

        Tier memory: every pick updates the per-tier "last picked"
        record so that future ``pick_tier_aware`` calls can avoid
        repeating the same id within a tier.
        """
        if not self._pool:
            return None
        exclude_set = set(exclude_recent)
        candidates = [a for a in self._pool if a.id not in exclude_set]
        if not candidates:
            candidates = list(self._pool)
        weights = [a.weight for a in candidates]
        pick = self.rng.choices(candidates, weights=weights, k=1)[0]
        self._last_per_tier[pick.rarity] = pick.id
        return pick

    def last_for_tier(self, rarity: float) -> str | None:
        """Return the last animation id picked from this tier (or None)."""
        return self._last_per_tier.get(rarity)

    def pick_tier_aware(self, expected_rarity: float | None = None) -> IdleAnimation | None:
        """Pick with tier-level no-repeat enforcement.

        Two-step draw:
          1) Pick a rarity tier weighted by total tier weight.
          2) Within that tier, exclude the last id picked from the
             same tier (if any) and pick a different one.

        ``expected_rarity`` is accepted for API compatibility but
        ignored — the tier is sampled fresh each call so every pick
        is correctly no-repeat-safe, including rare tiers like ``c``.
        """
        if not self._pool:
            return None
        # Aggregate weights per tier
        tier_weights: dict[float, float] = {}
        for a in self._pool:
            tier_weights[a.rarity] = tier_weights.get(a.rarity, 0.0) + a.weight
        if not tier_weights:
            return None
        tiers = list(tier_weights.keys())
        weights = [tier_weights[t] for t in tiers]
        tier = self.rng.choices(tiers, weights=weights, k=1)[0]
        # Now pick within this tier, excluding the last id from same tier.
        # IMPORTANT: we restrict candidates to this tier's pool so the
        # tier choice is respected (otherwise weighted-random across the
        # whole pool could pick an animation from a different tier).
        tier_pool = [a for a in self._pool if a.rarity == tier]
        last = self._last_per_tier.get(tier)
        exclude_set = {last} if last else set()
        candidates = [a for a in tier_pool if a.id not in exclude_set]
        if not candidates:
            # tier only has one member and it's the excluded one —
            # fall back to the full tier pool (repeat is unavoidable)
            candidates = list(tier_pool)
        weights_in = [a.weight for a in candidates]
        pick = self.rng.choices(candidates, weights=weights_in, k=1)[0]
        self._last_per_tier[pick.rarity] = pick.id
        return pick

    # ------------------------------------------------------------------ stats
    def expected_pick_rate(self, animation_id: str) -> float:
        """Fraction of picks expected to be this animation.

        Useful for logging or settings UI: shows the user that a c-tier
        animation will appear roughly once every N picks.
        """
        target = next((a for a in self._pool if a.id == animation_id), None)
        if target is None:
            return 0.0
        total = self.total_weight()
        if total == 0:
            return 0.0
        return target.weight / total


# --------------------------------------------------------------------- parser
def parse_rarity_from_filename(name: str) -> float:
    """Infer rarity tier from a video filename.

    Filenames produced for Nora's idle pool use the convention
    ``nora-idle-<a|b|c>-<rest>.mp4``.  The single letter between the
    second and third dash encodes the tier:
      - ``a`` -> rarity 1   (most frequent)
      - ``b`` -> rarity 4   (4x rarer than a)
      - ``c`` -> rarity 20  (20x rarer than a — reward tier)
    Other files default to rarity 1.

    Note: this only inspects the tier letter; the exact variant
    description after the tier is preserved as part of the animation id.
    """
    stem = name.lower().replace(" ", "-").replace("_", "-")
    parts = stem.split("-")
    # Expect at least: nora, idle, <tier>, ...
    if len(parts) >= 3 and parts[1] == "idle":
        tier = parts[2]
        if tier == "a":
            return 1.0
        if tier == "b":
            return 4.0
        if tier == "c":
            return 20.0
    return 1.0


def build_pool_from_videos(
    video_paths: Sequence, *, prefix: str = "nora"
) -> list[IdleAnimation]:
    """Turn a list of video paths into a weighted idle pool.

    The animation id is the filename stem with spaces and special chars
    normalised to underscores.  Rarity comes from the a/b/c tier
    convention described in :func:`parse_rarity_from_filename`.
    """
    pool: list[IdleAnimation] = []
    for p in video_paths:
        stem = p.stem.replace(" ", "-").replace("_", "-")
        # "nora-idle-a-sigh0" -> "nora_idle_a_sigh0"
        anim_id = stem.replace("-", "_")
        # strip redundant prefix duplication if any
        rarity = parse_rarity_from_filename(p.name)
        pool.append(IdleAnimation(id=anim_id, rarity=rarity, label=p.name))
    return pool
