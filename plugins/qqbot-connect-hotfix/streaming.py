"""Native QQ C2C streaming compatibility for Hermes gateways.

QQ's ``/v2/users/{openid}/stream_messages`` endpoint treats the stream as
the message: the first frame opens it, continuation frames replace the
visible body, and ``input_state=10`` seals the same message.  Hermes' generic
draft consumer can drive that lifecycle when the adapter advertises native
draft support and converts the turn-final ``send()`` into the sealing frame.

This module intentionally patches only C2C chats.  QQ group messages use a
different passive-reply contract and do not expose the C2C stream endpoint.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_STREAM_PATCHED = "_qqbot_native_c2c_streaming_patched"
_RUNNER_PATCHED = "_qqbot_native_c2c_streaming_runner_patched"
_OVERFLOW_PATCHED = "_qqbot_native_c2c_overflow_patched"
_COMMENTARY_PATCHED = "_qqbot_native_c2c_commentary_context_patched"
_TURN_FINAL_PATCHED = "_qqbot_native_c2c_turn_final_context_patched"
_MIN_HERMES_VERSION = (0, 20, 5)
_MAX_OPEN_STREAMS = 128
_MAX_COMPLETED_OWNERS_PER_CHAT = 256
_MAX_COMPLETED_OWNER_CHATS = 1024
_MAX_FINAL_ONLY_PENDING = 256
_MAX_FINAL_ONLY_PENDING_CHATS = 1024
_MAX_NATIVE_LANE_CHATS = 1024
_MAX_TYPING_ANCHORS = 1024
_MAX_FINAL_DELIVERY_CLAIMS = 128
_MAX_FINAL_DELIVERY_RESULTS = 1024
_NATIVE_STREAM_ACCUMULATION_LIMIT = 2**31 - 1
_NATIVE_STREAM_MAX_AGE_SECONDS = 480.0
_SEAL_RETRY_DELAYS = (0.0, 0.2, 0.8)
_FRAME_RETRY_DELAYS = (0.2, 0.8, 2.0, 5.0)
_COMPLETED_COMMENTARY_CONTEXT = contextvars.ContextVar(
    "qqbot_completed_commentary_context",
    default=None,
)
_TURN_FINAL_CONTEXT = contextvars.ContextVar(
    "qqbot_turn_final_context",
    default=None,
)


def _hermes_version_tuple() -> tuple[int, ...]:
    """Return the running Hermes version without consulting package metadata.

    Profile installs can place a newer distribution metadata record beside an
    older source checkout.  ``hermes_cli.__version__`` follows the code that is
    actually imported by the Gateway, which is the compatibility boundary that
    matters here.
    """

    try:
        from hermes_cli import __version__
    except Exception:
        return ()
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", str(__version__))
    if match is None:
        # Pre-releases (rc/dev/alpha/beta), local suffixes, and unknown version
        # shapes fail closed. The streaming contract is guaranteed only by a
        # stable Hermes release at or above the minimum.
        return ()
    return tuple(int(part) for part in match.groups())


def _hermes_streaming_supported() -> bool:
    version = _hermes_version_tuple()
    return bool(version) and version >= _MIN_HERMES_VERSION


def _send_result(*, success: bool, message_id=None, error=None, raw_response=None):
    from gateway.platforms.base import SendResult

    return SendResult(
        success=success,
        message_id=message_id,
        error=error,
        raw_response=raw_response,
        retryable=not success,
    )


@dataclass(frozen=True)
class _CompletedCommentaryContext:
    """Task-local identity for one Hermes completed-commentary callback."""

    adapter_identity: int
    chat_id: str
    anchor: str
    cleaned: str


@dataclass(frozen=True)
class _TurnFinalContext:
    """Task-local identity and actual delta provenance for finalization."""

    adapter_identity: int
    chat_id: str
    anchor: str
    delta_payload: str
    delta_content: str


@dataclass(frozen=True)
class _QQC2CAmbiguousFrame:
    """One submitted frame whose QQ response was lost in transport."""

    index: int
    content: str
    input_state: int


@dataclass
class _QQC2CStream:
    chat_id: str
    draft_id: int
    reply_to: str
    msg_seq: int
    stream_msg_id: Optional[str] = None
    next_index: int = 0
    last_content: str = ""
    committed_prefix: str = ""
    # Text in bubbles closed by steering is not ownership evidence for a
    # later, independently completed answer with the same words.
    display_prefix: str = ""
    last_completed_stream_id: Optional[str] = None
    opened_monotonic: Optional[float] = None
    # QQ may accept and display a frame while returning its terminal lifetime
    # error.  Such a carrier must never receive another index or seal request.
    retired: bool = False
    passive_reply_exhausted: bool = False
    ambiguous_frame: Optional[_QQC2CAmbiguousFrame] = None
    frame_failure_count: int = 0
    frame_retry_not_before: float = 0.0
    deferred_content: Optional[str] = None
    # Text successfully delivered by the immutable ordinary-message fallback.
    # It is already user-visible but can never be absorbed into a later native
    # replace/seal without displaying the same suffix twice.
    ordinary_owned_suffix: str = ""
    # A complete turn-final may already be visible even though QQ rejected
    # every seal attempt. Preserve its exact identity so a later cancellation
    # close can publish completion without treating an arbitrary partial draft
    # as a fully delivered final.
    close_pending_final_payload: Optional[str] = None
    close_pending_final_content: Optional[str] = None
    sealed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry_task: Optional[asyncio.Task] = field(default=None, repr=False)


@dataclass(frozen=True)
class _QQC2CTurnTombstone:
    """Bounded lifecycle evidence for a turn removed from the active maps."""

    chat_id: str
    draft_id: int
    reply_to: str
    final_payload: str
    final_content: str
    message_id: Optional[str] = None
    final_delivered: bool = True


@dataclass
class _QQC2CFinalDeliveryClaim:
    """One keyed single-flight retained until all registered callers exit."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    successful_result: Any = None
    transient_completion: Any = None
    cache_completed: bool = True
    attempt_task: Optional[asyncio.Task] = None


@dataclass(frozen=True)
class _QQC2CFinalAttemptOutcome:
    """Broker metadata kept out of the public platform ``SendResult``."""

    result: Any
    cache_completed: bool = True


def _final_attempt_parts(outcome) -> tuple[Any, bool]:
    if isinstance(outcome, _QQC2CFinalAttemptOutcome):
        return outcome.result, outcome.cache_completed
    return outcome, True


@dataclass(frozen=True)
class _QQC2CFinalDeliveryBrokerStats:
    """Read-only diagnostics for the bounded delivery broker."""

    active: int
    registered: int
    waiting: int
    completed: int
    peak: int
    limit: int


class _QQC2CFinalDeliveryBroker:
    """Bounded keyed single-flight for complete final ownership attempts.

    The broker deliberately knows nothing about QQ streams or tombstones.  It
    guarantees that one ``(chat_id, reply anchor)`` runs at most one delivery
    attempt at a time, shares the first successful result with every caller
    already registered on that flight, coordinates adjacent lifecycle
    mutations after that attempt, and applies backpressure before a new
    distinct key can enter the bounded registry.
    """

    def __init__(self, *, limit: int, completed_limit: int = 1024):
        self._limit = max(1, int(limit))
        self._completed_limit = max(1, int(completed_limit))
        self._slot_available = asyncio.Event()
        self._slot_available.set()
        self._claims: Dict[tuple[str, str], _QQC2CFinalDeliveryClaim] = {}
        self._completed: Dict[tuple[str, str], Any] = {}
        self._waiting = 0
        self._peak = 0

    def stats(self) -> _QQC2CFinalDeliveryBrokerStats:
        return _QQC2CFinalDeliveryBrokerStats(
            active=len(self._claims),
            registered=sum(claim.users for claim in self._claims.values()),
            waiting=self._waiting,
            completed=len(self._completed),
            peak=self._peak,
            limit=self._limit,
        )

    def registered_for(self, key) -> int:
        normalized_key = (str(key[0]), str(key[1]))
        claim = self._claims.get(normalized_key)
        return 0 if claim is None else claim.users

    def completed_for(self, key):
        """Return bounded anchor-scoped completion evidence, if retained."""

        normalized_key = (str(key[0]), str(key[1]))
        claim = self._claims.get(normalized_key)
        if claim is not None and claim.successful_result is not None:
            return claim.successful_result
        return self._completed_result(normalized_key)

    def transient_completion_for(self, key):
        """Return same-flight lifecycle evidence without extending its life."""

        normalized_key = (str(key[0]), str(key[1]))
        claim = self._claims.get(normalized_key)
        return None if claim is None else claim.transient_completion

    def remember_transient_completion(self, key, completion: Any) -> None:
        """Retain contextual completion only until registered users drain."""

        normalized_key = (str(key[0]), str(key[1]))
        claim = self._claims.get(normalized_key)
        if claim is not None:
            claim.transient_completion = completion

    async def _register(
        self,
        key: tuple[str, str],
    ) -> _QQC2CFinalDeliveryClaim:
        while True:
            # Registration and capacity checks contain no await, so one event
            # loop turn atomically joins an existing key or creates one slot.
            claim = self._claims.get(key)
            if claim is not None:
                claim.users += 1
                return claim
            if len(self._claims) < self._limit:
                claim = _QQC2CFinalDeliveryClaim()
                self._claims[key] = claim
                self._peak = max(self._peak, len(self._claims))
                claim.users = 1
                if len(self._claims) >= self._limit:
                    self._slot_available.clear()
                return claim

            # A released slot wakes every queued key. The first distinct key
            # claims the slot; every caller for that same key then sees and
            # joins its flight, while other keys return to bounded backpressure.
            self._waiting += 1
            try:
                await self._slot_available.wait()
            finally:
                self._waiting -= 1

    def _unregister(
        self,
        key: tuple[str, str],
        claim: _QQC2CFinalDeliveryClaim,
    ) -> None:
        claim.users -= 1
        self._cleanup(key, claim)

    def _cleanup(
        self,
        key: tuple[str, str],
        claim: _QQC2CFinalDeliveryClaim,
    ) -> None:
        if (
            claim.users == 0
            and claim.attempt_task is None
            and self._claims.get(key) is claim
        ):
            if claim.successful_result is not None and claim.cache_completed:
                self._remember_completed(key, claim.successful_result)
            self._claims.pop(key, None)
            self._slot_available.set()

    def _remember_completed(self, key: tuple[str, str], result: Any) -> None:
        self._completed.pop(key, None)
        self._completed[key] = result
        while len(self._completed) > self._completed_limit:
            self._completed.pop(next(iter(self._completed)))

    def _completed_result(self, key: tuple[str, str]):
        result = self._completed.pop(key, None)
        if result is not None:
            self._completed[key] = result
        return result

    def remember_completed(self, key, result: Any) -> None:
        """Publish completion produced outside a turn-final broker flight.

        Cancellation cleanup can finish sealing a previously delivered final.
        Recording that transition here keeps later same-anchor callbacks from
        redelivering after the independently bounded turn tombstone is evicted.
        """

        if not getattr(result, "success", False):
            return
        normalized_key = (str(key[0]), str(key[1]))
        self._remember_completed(normalized_key, result)
        claim = self._claims.get(normalized_key)
        if claim is not None:
            claim.successful_result = result
            claim.cache_completed = True

    def _attempt_finished(
        self,
        key: tuple[str, str],
        claim: _QQC2CFinalDeliveryClaim,
        task: asyncio.Task,
    ) -> None:
        if claim.attempt_task is not task:
            return
        claim.attempt_task = None
        if not task.cancelled():
            try:
                outcome = task.result()
            except Exception:
                outcome = None
            result, cache_completed = _final_attempt_parts(outcome)
            if getattr(result, "success", False):
                claim.successful_result = result
                claim.cache_completed = cache_completed
        self._cleanup(key, claim)

    async def run(self, key, operation):
        normalized_key = (str(key[0]), str(key[1]))
        completed = self._completed_result(normalized_key)
        if completed is not None:
            return completed
        claim = await self._register(normalized_key)
        try:
            async with claim.lock:
                while True:
                    completed = self._completed_result(normalized_key)
                    if completed is not None:
                        return completed
                    if claim.successful_result is not None:
                        return claim.successful_result
                    task = claim.attempt_task
                    created = task is None
                    if created:
                        task = asyncio.create_task(operation())
                        claim.attempt_task = task
                        task.add_done_callback(
                            lambda done,
                            broker=self,
                            flight_key=normalized_key,
                            flight=claim: broker._attempt_finished(
                                flight_key,
                                flight,
                                done,
                            )
                        )
                    try:
                        outcome = await asyncio.shield(task)
                    except Exception:
                        if claim.attempt_task is task:
                            claim.attempt_task = None
                        if created:
                            raise
                        # This caller inherited an in-flight attempt whose
                        # original caller left. It now owns the definite-failure
                        # handoff and may start one fresh attempt.
                        continue
                    result, cache_completed = _final_attempt_parts(outcome)
                    if getattr(result, "success", False):
                        # Store before releasing the per-key lock. Every caller
                        # already counted in ``users`` can now complete without
                        # consulting an independently evictable tombstone.
                        claim.successful_result = result
                        claim.cache_completed = cache_completed
                        return result
                    if claim.attempt_task is task:
                        claim.attempt_task = None
                    if created:
                        return result
                    # A waiter inherited an in-flight attempt from a cancelled
                    # caller. Only a definite unsuccessful result may hand off
                    # to one new external attempt.
        finally:
            self._unregister(normalized_key, claim)

    async def coordinate(self, key, operation):
        """Run a lifecycle mutation after any same-key final attempt settles.

        Unlike ``run()``, coordination never replays a completed delivery or
        returns the flight's successful result in place of ``operation``.
        Cancellation cleanup and late draft frames must inspect the resulting
        stream state, but cannot race the external send owned by the active
        final flight.
        """

        normalized_key = (str(key[0]), str(key[1]))
        claim = await self._register(normalized_key)
        try:
            async with claim.lock:
                task = claim.attempt_task
                if task is not None:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if not task.cancelled():
                            raise
                    except Exception:
                        # Definite final-attempt failure leaves the lifecycle
                        # state for this cleanup operation to resolve.
                        pass
                return await operation()
        finally:
            self._unregister(normalized_key, claim)


def _stream_maps(adapter):
    streams = getattr(adapter, "_qq_native_c2c_streams", None)
    if streams is None:
        streams = {}
        adapter._qq_native_c2c_streams = streams
    anchors = getattr(adapter, "_qq_native_c2c_streams_by_anchor", None)
    if anchors is None:
        anchors = {}
        adapter._qq_native_c2c_streams_by_anchor = anchors
    return streams, anchors


def _stream_key(chat_id: str, draft_id: int) -> tuple[str, int]:
    """Return the adapter-contract identity for one active draft."""

    return str(chat_id), int(draft_id)


def _final_delivery_broker(adapter) -> _QQC2CFinalDeliveryBroker:
    broker = getattr(adapter, "_qq_native_c2c_final_delivery_broker", None)
    if broker is None:
        broker = _QQC2CFinalDeliveryBroker(
            limit=_MAX_FINAL_DELIVERY_CLAIMS,
            completed_limit=_MAX_FINAL_DELIVERY_RESULTS,
        )
        adapter._qq_native_c2c_final_delivery_broker = broker
    return broker


def _turn_tombstones(
    adapter,
) -> Dict[str, Dict[tuple[str, int], _QQC2CTurnTombstone]]:
    owners = getattr(adapter, "_qq_native_c2c_completed_owners", None)
    if owners is None:
        owners = {}
        adapter._qq_native_c2c_completed_owners = owners
    return owners


def _remember_turn_tombstone(
    adapter,
    state: _QQC2CStream,
    *,
    final_payload: str,
    final_content: str,
    final_delivered: bool = True,
) -> _QQC2CTurnTombstone:
    """Retain exact turn lifecycle evidence without growing indefinitely."""

    owners = _turn_tombstones(adapter)
    bucket = owners.pop(state.chat_id, None)
    if bucket is None:
        bucket = {}
    owners[state.chat_id] = bucket
    key = (state.reply_to, state.draft_id)
    bucket.pop(key, None)
    owner = _QQC2CTurnTombstone(
        chat_id=state.chat_id,
        draft_id=state.draft_id,
        reply_to=state.reply_to,
        final_payload=str(final_payload or ""),
        final_content=str(final_content or ""),
        message_id=state.stream_msg_id or state.last_completed_stream_id,
        final_delivered=final_delivered,
    )
    bucket[key] = owner
    while len(bucket) > _MAX_COMPLETED_OWNERS_PER_CHAT:
        bucket.pop(next(iter(bucket)))
    while len(owners) > _MAX_COMPLETED_OWNER_CHATS:
        owners.pop(next(iter(owners)))
    return owner


def _remember_transient_turn_completion(
    adapter,
    owner: _QQC2CTurnTombstone,
) -> None:
    """Keep abandon-first ownership until same-anchor waiters drain."""

    _final_delivery_broker(adapter).remember_transient_completion(
        (owner.chat_id, owner.reply_to),
        owner,
    )


def _ordinary_owned_final_content(state: _QQC2CStream) -> str:
    """Return the two immutable carriers' complete visible final content."""

    return f"{_visible_stream_content(state)}{state.ordinary_owned_suffix}"


def _publish_external_turn_completion(
    adapter,
    state: _QQC2CStream,
    result,
    *,
    final_payload: str,
    final_content: str,
) -> None:
    """Persist a delivered completion produced outside ``broker.run``."""

    if not getattr(result, "success", False):
        return
    _remember_turn_tombstone(
        adapter,
        state,
        final_payload=final_payload,
        final_content=final_content,
    )
    _final_delivery_broker(adapter).remember_completed(
        (state.chat_id, state.reply_to),
        result,
    )


def _completed_owner_for_draft(
    adapter,
    *,
    chat_id: str,
    reply_to: str,
    draft_id: int,
) -> Optional[_QQC2CTurnTombstone]:
    owners = _turn_tombstones(adapter)
    chat_key = str(chat_id)
    bucket = owners.get(chat_key, {})
    owner = bucket.get((str(reply_to), int(draft_id)))
    if owner is not None:
        owners.pop(chat_key, None)
        owners[chat_key] = bucket
    return owner


def _completed_owner_for_final(
    adapter,
    *,
    chat_id: str,
    reply_to: str,
    content: str,
) -> Optional[_QQC2CTurnTombstone]:
    payload = str(content or "")
    owners = _turn_tombstones(adapter)
    chat_key = str(chat_id)
    bucket = owners.get(chat_key, {})
    for owner in reversed(tuple(bucket.values())):
        if _turn_tombstone_owns_final(
            owner,
            chat_id=str(chat_id),
            reply_to=str(reply_to),
            payload=payload,
        ):
            owners.pop(chat_key, None)
            owners[chat_key] = bucket
            return owner
    return None


def _turn_tombstone_owns_final(
    owner: _QQC2CTurnTombstone,
    *,
    chat_id: str,
    reply_to: str,
    payload: str,
) -> bool:
    """Match one delivered owner without broadening partial cancellation."""

    return bool(
        owner.final_delivered
        and owner.chat_id == str(chat_id)
        and owner.reply_to == str(reply_to)
        and (
            payload in (owner.final_payload, owner.final_content)
            or _terminal_payload_is_owned(owner.final_content, payload)
        )
    )


def _cancelled_owner_for_anchor(
    adapter,
    *,
    chat_id: str,
    reply_to: str,
) -> Optional[_QQC2CTurnTombstone]:
    """Return cancellation evidence that still needs one visible final."""

    owners = _turn_tombstones(adapter)
    chat_key = str(chat_id)
    bucket = owners.get(chat_key, {})
    for owner in reversed(tuple(bucket.values())):
        if (
            not owner.final_delivered
            and owner.chat_id == chat_key
            and owner.reply_to == str(reply_to)
        ):
            owners.pop(chat_key, None)
            owners[chat_key] = bucket
            return owner
    return None


def _promote_cancelled_owner(
    adapter,
    owner: _QQC2CTurnTombstone,
    *,
    final_content: str,
    message_id: Optional[str],
) -> None:
    """Record that a formerly cancelled turn now owns one delivered final."""

    owners = _turn_tombstones(adapter)
    bucket = owners.get(owner.chat_id, {})
    key = (owner.reply_to, owner.draft_id)
    if bucket.get(key) != owner:
        return
    payload = str(final_content or "")
    bucket[key] = _QQC2CTurnTombstone(
        chat_id=owner.chat_id,
        draft_id=owner.draft_id,
        reply_to=owner.reply_to,
        final_payload=payload,
        final_content=payload,
        message_id=message_id,
        final_delivered=True,
    )
    owners.pop(owner.chat_id, None)
    owners[owner.chat_id] = bucket


def _final_only_pending(adapter) -> Dict[str, Dict[tuple[str, int], _QQC2CStream]]:
    pending = getattr(adapter, "_qq_native_c2c_final_only_pending", None)
    if pending is None:
        pending = {}
        adapter._qq_native_c2c_final_only_pending = pending
    return pending


def _remember_final_only_pending(adapter, state: _QQC2CStream) -> None:
    pending = _final_only_pending(adapter)
    bucket = pending.pop(state.chat_id, None)
    if bucket is None:
        bucket = {}
    pending[state.chat_id] = bucket
    key = (state.reply_to, state.draft_id)
    bucket.pop(key, None)
    bucket[key] = state
    while len(bucket) > _MAX_FINAL_ONLY_PENDING:
        bucket.pop(next(iter(bucket)))
    while len(pending) > _MAX_FINAL_ONLY_PENDING_CHATS:
        pending.pop(next(iter(pending)))


def _final_only_pending_for_draft(
    adapter,
    *,
    chat_id: str,
    reply_to: str,
    draft_id: int,
) -> Optional[_QQC2CStream]:
    pending = _final_only_pending(adapter)
    chat_key = str(chat_id)
    bucket = pending.get(chat_key, {})
    state = bucket.get((str(reply_to), int(draft_id)))
    if state is not None:
        pending.pop(chat_key, None)
        pending[chat_key] = bucket
    return state


def _final_only_pending_for_anchor(
    adapter,
    *,
    chat_id: str,
    reply_to: str,
) -> Optional[_QQC2CStream]:
    pending = _final_only_pending(adapter)
    chat_key = str(chat_id)
    bucket = pending.get(chat_key, {})
    for (owner_reply_to, _draft_id), state in reversed(tuple(bucket.items())):
        if owner_reply_to == str(reply_to):
            pending.pop(chat_key, None)
            pending[chat_key] = bucket
            return state
    return None


def _remove_final_only_pending(adapter, state: _QQC2CStream) -> None:
    pending = _final_only_pending(adapter)
    bucket = pending.get(state.chat_id)
    if bucket is None:
        return
    bucket.pop((state.reply_to, state.draft_id), None)
    if not bucket:
        pending.pop(state.chat_id, None)


def _remove_stream(adapter, state: _QQC2CStream) -> None:
    _cancel_stream_expiry(state)
    streams, anchors = _stream_maps(adapter)
    state_key = _stream_key(state.chat_id, state.draft_id)
    streams.pop(state_key, None)
    anchor_key = (state.chat_id, state.reply_to)
    if anchors.get(anchor_key) == state_key:
        anchors.pop(anchor_key, None)
    _native_lane_chats(adapter)


def _evict_unopened_streams(adapter, *, limit: int) -> None:
    """Reclaim only streams that never became visible on QQ.

    An opened stream must remain addressable until it is sealed.  Silently
    dropping its local state can strand a client-visible message in the
    generating state.  If all slots are opened, the new turn stays final-only
    instead of sacrificing an existing stream.
    """

    streams, anchors = _stream_maps(adapter)
    while len(streams) > max(0, limit):
        removable = next(
            (
                state
                for state in streams.values()
                if not state.stream_msg_id and not state.lock.locked()
            ),
            None,
        )
        if removable is None:
            break
        _remove_stream(adapter, removable)
    # Defensive cleanup for anchors whose stream was removed independently.
    for anchor_key, draft_id in list(anchors.items()):
        if draft_id not in streams:
            anchors.pop(anchor_key, None)


def _native_lane_chats(adapter) -> Dict[str, None]:
    chats = getattr(adapter, "_qq_native_c2c_lane_chats", None)
    if chats is None:
        chats = {}
        adapter._qq_native_c2c_lane_chats = chats
    elif isinstance(chats, set):
        # Accept an adapter created by an older in-process patch while
        # upgrading the lifetime-wide set to the bounded LRU representation.
        chats = dict.fromkeys(str(chat_id) for chat_id in chats)
        adapter._qq_native_c2c_lane_chats = chats
    _prune_native_lane_chats(adapter, chats)
    return chats


def _prune_native_lane_chats(
    adapter,
    chats: Dict[str, None],
) -> None:
    streams, _anchors = _stream_maps(adapter)
    active_chats = {state.chat_id for state in streams.values()}
    while len(chats) > max(0, _MAX_NATIVE_LANE_CHATS):
        removable = next(
            (chat_id for chat_id in chats if chat_id not in active_chats),
            None,
        )
        if removable is None:
            # Open native streams must stay selectable until sealed. The
            # registry converges as soon as one is removed.
            break
        chats.pop(removable, None)


def _mark_native_lane(adapter, chat_id: str) -> None:
    if chat_id:
        chats = _native_lane_chats(adapter)
        chat_key = str(chat_id)
        chats.pop(chat_key, None)
        chats[chat_key] = None
        _prune_native_lane_chats(adapter, chats)


def _unmark_native_lane(adapter, chat_id: str) -> None:
    if chat_id:
        chats = _native_lane_chats(adapter)
        chats.pop(str(chat_id), None)


def _typing_budget_applies(adapter, chat_id: str) -> bool:
    chat_id = str(chat_id)
    if chat_id in _native_lane_chats(adapter):
        return True
    streams, _anchors = _stream_maps(adapter)
    return any(state.chat_id == chat_id for state in streams.values())


def _resolved_platform_streaming_enabled(source, scfg) -> bool:
    """Mirror Hermes' global + per-platform streaming resolution.

    Hermes can create a ``GatewayStreamConsumer`` solely for interim assistant
    messages even when streaming itself is disabled. The native QQ lane must
    therefore use the already-resolved display setting, not consumer creation
    as evidence that streaming was enabled.
    """

    global_enabled = bool(
        getattr(scfg, "enabled", False)
        and str(getattr(scfg, "transport", "auto") or "auto").lower() != "off"
    )
    try:
        from gateway.display_config import resolve_display_setting
        from gateway.run import _load_gateway_config, _platform_config_key

        platform_key = _platform_config_key(getattr(source, "platform", "qqbot"))
        override = resolve_display_setting(
            _load_gateway_config(),
            platform_key,
            "streaming",
        )
    except Exception:
        logger.debug(
            "qqbot-connect-hotfix: could not resolve per-platform streaming; "
            "native QQ streaming stays disabled",
            exc_info=True,
        )
        return False
    return global_enabled if override is None else bool(override)


def _patch_gateway_stream_gate(QQAdapter) -> str:
    """Let native QQ C2C drafts pass Hermes' legacy edit-only gate.

    Hermes currently rejects every non-editable adapter before
    ``GatewayStreamConsumer`` can ask whether it supports a native draft
    transport. QQ cannot edit ordinary messages, but its C2C stream endpoint
    is exactly such a native transport. Narrow the exception to QQ C2C only;
    groups and every other non-editable platform keep the upstream guard.
    """

    try:
        from gateway.run import GatewayRunner
    except ImportError as exc:
        logger.warning(
            "qqbot-connect-hotfix: could not patch Gateway streaming gate: %s",
            exc,
        )
        return "QQ C2C Gateway streaming gate unavailable"

    original_build = GatewayRunner._build_stream_consumer_config
    if getattr(original_build, _RUNNER_PATCHED, False):
        return "QQ C2C Gateway streaming gate already patched"

    @functools.wraps(original_build)
    def _build_stream_consumer_config(
        self,
        source,
        scfg,
        adapter,
        *,
        on_missing_cursor: str,
    ):
        if isinstance(adapter, QQAdapter):
            chat_id = str(getattr(source, "chat_id", "") or "")
            native_c2c = (
                _resolved_platform_streaming_enabled(source, scfg)
                and _is_c2c(
                    adapter,
                    chat_id,
                    getattr(source, "chat_type", "") or None,
                )
            )
            if native_c2c:
                _mark_native_lane(adapter, chat_id)
            else:
                # Config is resolved for every new turn. Revoke a lane that
                # was selected before a live enabled -> disabled transition;
                # any already-open stream remains protected by _stream_maps.
                _unmark_native_lane(adapter, chat_id)
            if (
                native_c2c
                and on_missing_cursor == "raise"
                and not getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
            ):
                config, pause_typing = original_build(
                    self,
                    source,
                    scfg,
                    adapter,
                    on_missing_cursor="fallback",
                )
                # QQ renders its own native generating state. A text cursor
                # is unnecessary and would break replace-prefix stability.
                config.cursor = ""
                return config, pause_typing

        return original_build(
            self,
            source,
            scfg,
            adapter,
            on_missing_cursor=on_missing_cursor,
        )

    setattr(_build_stream_consumer_config, _RUNNER_PATCHED, True)
    GatewayRunner._build_stream_consumer_config = _build_stream_consumer_config
    return "QQ C2C Gateway streaming gate patched"


def _patch_gateway_overflow_limit(QQAdapter) -> str:
    """Keep QQ C2C cumulative text intact for adapter-owned rollover.

    Hermes' generic overflow path seals a head as an ordinary message and
    resets its accumulator to the tail. QQ native replace streams instead need
    each active stream to retain its own accepted prefix. Defer splitting only
    for an active QQ C2C native lane; the adapter then seals full stream chunks
    and opens a fresh stream for the remaining cumulative suffix.
    """

    try:
        from gateway.stream_consumer import GatewayStreamConsumer
    except ImportError as exc:
        logger.warning(
            "qqbot-connect-hotfix: could not patch native overflow limit: %s",
            exc,
        )
        return "QQ C2C native overflow patch unavailable"

    original_limit = GatewayStreamConsumer._raw_message_limit
    if getattr(original_limit, _OVERFLOW_PATCHED, False):
        return "QQ C2C native overflow already patched"

    @functools.wraps(original_limit)
    def _raw_message_limit(self):
        base = original_limit(self)
        adapter = getattr(self, "adapter", None)
        chat_id = str(getattr(self, "chat_id", "") or "")
        if (
            isinstance(adapter, QQAdapter)
            and chat_id in _native_lane_chats(adapter)
            and _is_c2c(adapter, chat_id)
        ):
            return max(int(base), _NATIVE_STREAM_ACCUMULATION_LIMIT)
        return base

    setattr(_raw_message_limit, _OVERFLOW_PATCHED, True)
    GatewayStreamConsumer._raw_message_limit = _raw_message_limit
    return "QQ C2C native overflow patched"


def _patch_gateway_commentary_context(QQAdapter) -> str:
    """Identify the completed commentary callback that owns streamed deltas.

    Consecutive Codex commentary items can be concatenated without a textual
    boundary. A task-local context distinguishes that trusted consumer callback
    from unrelated interim sends while avoiding any wire metadata change.
    """

    try:
        from gateway.stream_consumer import GatewayStreamConsumer
    except ImportError as exc:
        logger.warning(
            "qqbot-connect-hotfix: could not patch commentary context: %s",
            exc,
        )
        return "QQ C2C commentary context unavailable"

    original_send_commentary = GatewayStreamConsumer._send_commentary
    if getattr(original_send_commentary, _COMMENTARY_PATCHED, False):
        return "QQ C2C commentary context already patched"

    @functools.wraps(original_send_commentary)
    async def _send_commentary(self, text: str):
        adapter = getattr(self, "adapter", None)
        chat_id = str(getattr(self, "chat_id", "") or "")
        if (
            not isinstance(adapter, QQAdapter)
            or chat_id not in _native_lane_chats(adapter)
            or not _is_c2c(adapter, chat_id)
        ):
            return await original_send_commentary(self, text)

        cleaned = str(self._clean_for_display(text) or "")
        # A completed commentary item is no longer a candidate final segment,
        # even when its terminal characters happen to match a later final.
        self._qq_native_final_delta_segment = ""
        anchor = str(getattr(self, "_initial_reply_to_id", "") or "")
        token = _COMPLETED_COMMENTARY_CONTEXT.set(
            _CompletedCommentaryContext(
                adapter_identity=id(adapter),
                chat_id=chat_id,
                anchor=anchor,
                cleaned=cleaned,
            )
        )
        try:
            return await original_send_commentary(self, text)
        finally:
            _COMPLETED_COMMENTARY_CONTEXT.reset(token)

    setattr(_send_commentary, _COMMENTARY_PATCHED, True)
    GatewayStreamConsumer._send_commentary = _send_commentary
    return "QQ C2C commentary context patched"


def _patch_gateway_turn_final_context(QQAdapter) -> str:
    """Bind finalization to the filtered deltas of the unfinished segment.

    Hermes can replace its cumulative consumer buffer with ``final_response``
    immediately before finalization. When the same final was already emitted
    as deltas directly after commentary, no textual token boundary separates
    the two phases. Callback identity alone is NOT delta provenance. Record
    only text appended by the think-filtered delta drain, before finish()
    replaces the upstream ledger. Completed commentary and tool boundaries
    end the candidate segment. All state is consumer-local and updated by the
    drain task, not the cross-thread producer callbacks.
    """

    try:
        from gateway.stream_consumer import (
            GatewayStreamConsumer,
            ensure_closed_code_fences,
        )
    except ImportError as exc:
        logger.warning(
            "qqbot-connect-hotfix: could not patch turn-final context: %s",
            exc,
        )
        return "QQ C2C turn-final context unavailable"

    original_send_or_edit = GatewayStreamConsumer._send_or_edit
    if getattr(original_send_or_edit, _TURN_FINAL_PATCHED, False):
        return "QQ C2C turn-final context already patched"

    original_append = GatewayStreamConsumer._append_accumulated

    def _eligible(self):
        adapter = getattr(self, "adapter", None)
        chat_id = str(getattr(self, "chat_id", "") or "")
        return bool(
            getattr(self, "_use_draft_streaming", False)
            and isinstance(adapter, QQAdapter)
            and chat_id in _native_lane_chats(adapter)
            and _is_c2c(adapter, chat_id)
        )

    @functools.wraps(original_append)
    def _append_accumulated(self, text: str):
        original_append(self, text)
        if text and _eligible(self):
            self._qq_native_final_delta_segment = (
                getattr(self, "_qq_native_final_delta_segment", "") + text
            )
            self._qq_native_final_delta_content = self._stream_ledger

    @functools.wraps(original_send_or_edit)
    async def _send_or_edit(
        self,
        text: str,
        *,
        finalize: bool = False,
        is_turn_final: bool = True,
    ):
        adapter = getattr(self, "adapter", None)
        chat_id = str(getattr(self, "chat_id", "") or "")
        trusted = bool(
            finalize
            and is_turn_final
            and _eligible(self)
        )
        if not trusted:
            if finalize and not is_turn_final and _eligible(self):
                self._qq_native_final_delta_segment = ""
            return await original_send_or_edit(
                self,
                text,
                finalize=finalize,
                is_turn_final=is_turn_final,
            )

        token = _TURN_FINAL_CONTEXT.set(
            _TurnFinalContext(
                adapter_identity=id(adapter),
                chat_id=chat_id,
                anchor=str(getattr(self, "_initial_reply_to_id", "") or ""),
                delta_payload=ensure_closed_code_fences(self._clean_for_display(
                    getattr(self, "_qq_native_final_delta_segment", "")
                )),
                delta_content=ensure_closed_code_fences(self._clean_for_display(
                    getattr(self, "_qq_native_final_delta_content", "")
                )),
            )
        )
        try:
            return await original_send_or_edit(
                self,
                text,
                finalize=finalize,
                is_turn_final=is_turn_final,
            )
        finally:
            _TURN_FINAL_CONTEXT.reset(token)

    setattr(_send_or_edit, _TURN_FINAL_PATCHED, True)
    GatewayStreamConsumer._append_accumulated = _append_accumulated
    GatewayStreamConsumer._send_or_edit = _send_or_edit
    return "QQ C2C turn-final context patched"


def _reply_anchor(metadata: Optional[Dict[str, Any]]) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("reply_to_message_id") or "").strip()


def _is_c2c(adapter, chat_id: str, chat_type: Optional[str] = None) -> bool:
    try:
        # QQ uses source.chat_type="dm" for both C2C and guild direct
        # messages. The adapter route map is the authoritative distinction:
        # only "c2c" supports /v2/users/{openid}/stream_messages.
        return str(adapter._guess_chat_type(chat_id)).lower() == "c2c"
    except Exception:
        # A literal c2c value remains a safe compatibility fallback for test
        # or relay adapters that do not expose QQ's route helper. Generic
        # dm/private values are intentionally insufficient.
        return str(chat_type or "").strip().lower() == "c2c"


def _stream_body(
    adapter,
    state: _QQC2CStream,
    content: str,
    *,
    input_state: int,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "input_mode": "replace",
        "input_state": input_state,
        "index": state.next_index,
        "content_type": (
            "markdown" if getattr(adapter, "_markdown_support", False) else "text"
        ),
        "content_raw": str(content or "")[: getattr(adapter, "MAX_MESSAGE_LENGTH", 4000)],
        "msg_id": state.reply_to,
        "msg_seq": state.msg_seq,
    }
    if state.stream_msg_id:
        body["stream_msg_id"] = state.stream_msg_id
    return body


def _native_stream_now(adapter) -> float:
    """Return an injectable monotonic clock for stream lifetime decisions."""

    clock = getattr(adapter, "_qq_native_stream_clock", time.monotonic)
    return float(clock())


def _native_stream_max_age(adapter) -> float:
    """Return the proactive QQ stream rollover age in seconds."""

    value = float(
        getattr(
            adapter,
            "_qq_native_stream_max_age_seconds",
            _NATIVE_STREAM_MAX_AGE_SECONDS,
        )
    )
    if value <= 0:
        raise RuntimeError("QQ native stream max age must be positive")
    return value


def _stream_frame_retry_delays(adapter) -> tuple[float, ...]:
    """Return the bounded per-carrier cooldown sequence."""

    values = tuple(
        float(value)
        for value in getattr(
            adapter,
            "_qq_native_stream_frame_retry_delays",
            _FRAME_RETRY_DELAYS,
        )
    )
    if not values or any(value < 0 for value in values):
        raise RuntimeError("QQ stream frame retry delays must be non-negative")
    return values


def _defer_stream_frame(adapter, state: _QQC2CStream, content: str) -> None:
    """Coalesce one failed cumulative frame behind a bounded cooldown."""

    delays = _stream_frame_retry_delays(adapter)
    state.frame_failure_count += 1
    delay = delays[min(state.frame_failure_count - 1, len(delays) - 1)]
    state.frame_retry_not_before = _native_stream_now(adapter) + delay
    state.deferred_content = str(content or "")


def _clear_stream_frame_cooldown(
    state: _QQC2CStream,
    *,
    keep_deferred: bool = False,
) -> None:
    """Reset carrier backoff after a successful request or carrier rollover."""

    state.frame_failure_count = 0
    state.frame_retry_not_before = 0.0
    if not keep_deferred:
        state.deferred_content = None


def _cancel_stream_expiry(state: _QQC2CStream) -> None:
    """Cancel the independent carrier deadline without cancelling itself."""

    task = state.expiry_task
    state.expiry_task = None
    if task is None or task.done():
        return
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    if task is not current:
        task.cancel()


async def _expire_stream_at_deadline(adapter, state: _QQC2CStream) -> None:
    """Seal or retire one carrier even when its turn emits no new delta."""

    sleeper = getattr(adapter, "_qq_native_stream_sleep", asyncio.sleep)
    try:
        while True:
            opened = state.opened_monotonic
            if opened is None:
                return
            remaining = (
                opened
                + _native_stream_max_age(adapter)
                - _native_stream_now(adapter)
            )
            if remaining <= 0:
                break
            await sleeper(remaining)

        async with state.lock:
            streams, _anchors = _stream_maps(adapter)
            if (
                streams.get(_stream_key(state.chat_id, state.draft_id)) is not state
                or state.sealed
                or state.retired
                or not state.stream_msg_id
                or state.opened_monotonic is None
            ):
                return
            head = str(state.last_content or "")
            data, seal_error = await _post_seal_with_retries(
                adapter,
                state,
                head,
            )
            if seal_error is not None:
                if not state.retired:
                    state.retired = True
                logger.warning(
                    "qqbot-connect-hotfix: QQ C2C silent carrier retired "
                    "at age deadline for chat=%s draft=%s: %s",
                    state.chat_id,
                    state.draft_id,
                    seal_error,
                )
                return

            committed = state.committed_prefix + head
            completed_id = state.stream_msg_id or state.last_completed_stream_id
            age = _native_stream_now(adapter) - state.opened_monotonic
            # Reuse the same state and lock so a draft already waiting behind
            # this timer observes the new unopened carrier atomically.
            state.msg_seq = int(adapter._next_msg_seq(state.reply_to))
            state.stream_msg_id = None
            state.next_index = 0
            state.last_content = ""
            state.committed_prefix = committed
            state.last_completed_stream_id = completed_id
            state.opened_monotonic = None
            state.sealed = False
            _clear_stream_frame_cooldown(state, keep_deferred=True)
            logger.info(
                "qqbot-connect-hotfix: QQ C2C silent age rollover sealed "
                "draft=%s committed=%s age=%.1fs",
                state.draft_id,
                len(committed),
                age,
            )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.exception(
            "qqbot-connect-hotfix: QQ C2C expiry task failed for chat=%s "
            "draft=%s: %s",
            state.chat_id,
            state.draft_id,
            exc,
        )
        async with state.lock:
            streams, _anchors = _stream_maps(adapter)
            if streams.get(_stream_key(state.chat_id, state.draft_id)) is state:
                state.retired = True
    finally:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if state.expiry_task is current:
            state.expiry_task = None


def _schedule_stream_expiry(adapter, state: _QQC2CStream) -> None:
    """Arm the independent lifetime task for a newly opened carrier."""

    _cancel_stream_expiry(state)
    state.expiry_task = asyncio.create_task(
        _expire_stream_at_deadline(adapter, state),
        name=f"qq-c2c-expiry-{state.draft_id}",
    )


def _is_terminal_stream_lifetime_error(exc: Exception) -> bool:
    """Recognize QQ's observed terminal C2C carrier-lifetime response."""

    return "同一流式消息发送超过时间限制" in str(exc)


def _is_terminal_passive_reply_budget_error(exc: Exception) -> bool:
    """Recognize QQ's terminal response-window or reply-budget rejection."""

    message = str(exc)
    return (
        "回复消息失败，被动回复时间或者次数超过限制" in message
        or "40034128" in message
    )


def _is_stale_stream_index_error(exc: Exception) -> bool:
    """Return whether QQ confirms that the submitted index was consumed."""

    return "请求参数index需要递增" in str(exc)


def _is_ambiguous_stream_transport_error(exc: Exception) -> bool:
    """Recognize failures where QQ may have consumed the request body."""

    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if type(current).__name__ in {
            "ConnectError",
            "NetworkError",
            "ReadError",
            "RemoteProtocolError",
            "TimeoutException",
            "WriteError",
        }:
            return True
        current = current.__cause__ or current.__context__
    return "QQ Bot API timeout [" in str(exc)


def _record_accepted_stream_frame(
    adapter,
    state: _QQC2CStream,
    body: Dict[str, Any],
    data: Optional[Dict[str, Any]],
) -> None:
    """Advance local ownership after QQ accepted one exact frame."""

    response_id = str((data or {}).get("id") or "").strip()
    first_frame = state.next_index == 0
    if first_frame and not response_id:
        raise RuntimeError("QQ stream first frame did not return stream_msg_id")
    if response_id:
        state.stream_msg_id = response_id
    if first_frame:
        state.opened_monotonic = _native_stream_now(adapter)
        logger.info(
            "qqbot-connect-hotfix: QQ C2C stream opened draft=%s",
            state.draft_id,
        )
    else:
        logger.debug(
            "qqbot-connect-hotfix: QQ C2C stream frame draft=%s index=%s",
            state.draft_id,
            state.next_index,
        )
    state.next_index += 1
    state.last_content = str(body["content_raw"])
    if first_frame:
        _schedule_stream_expiry(adapter, state)


async def _reconcile_ambiguous_stream_frame(
    adapter,
    state: _QQC2CStream,
) -> None:
    """Resolve one lost response without repeatedly reusing its index."""

    pending = state.ambiguous_frame
    if pending is None:
        return
    body = _stream_body(
        adapter,
        state,
        pending.content,
        input_state=pending.input_state,
    )
    if int(body["index"]) != pending.index:
        state.retired = True
        state.ambiguous_frame = None
        _cancel_stream_expiry(state)
        raise RuntimeError("QQ ambiguous stream index changed before reconciliation")
    try:
        data = await adapter._api_request(
            "POST",
            f"/v2/users/{state.chat_id}/stream_messages",
            body,
        )
    except Exception as exc:
        if _is_stale_stream_index_error(exc) and state.stream_msg_id:
            # The only bounded retry proves the first request was accepted.
            # Promote that submitted body locally and continue at index + 1.
            _record_accepted_stream_frame(adapter, state, body, None)
            state.ambiguous_frame = None
            logger.info(
                "qqbot-connect-hotfix: reconciled accepted QQ C2C frame "
                "after lost response for chat=%s draft=%s index=%s",
                state.chat_id,
                state.draft_id,
                pending.index,
            )
            return
        # A second inconclusive result cannot be retried safely. Retire the
        # carrier and keep last_content at the last acknowledged body so final
        # fallback preserves every potentially unseen character.
        state.retired = True
        state.ambiguous_frame = None
        _cancel_stream_expiry(state)
        raise
    _record_accepted_stream_frame(adapter, state, body, data)
    state.ambiguous_frame = None


async def _post_stream_frame(
    adapter,
    state: _QQC2CStream,
    content: str,
    *,
    input_state: int,
):
    if state.ambiguous_frame is not None:
        pending = state.ambiguous_frame
        await _reconcile_ambiguous_stream_frame(adapter, state)
        if state.retired:
            raise RuntimeError("QQ stream retired after ambiguous transport failure")
        submitted = str(content or "")[: getattr(adapter, "MAX_MESSAGE_LENGTH", 4000)]
        if pending.input_state == input_state and pending.content == submitted:
            # Reconciliation already accepted this exact request, including a
            # seal frame. Returning its carrier id avoids a redundant next
            # index while preserving the caller's normal success path.
            return {"id": state.stream_msg_id} if state.stream_msg_id else {}
    body = _stream_body(adapter, state, content, input_state=input_state)
    try:
        data = await adapter._api_request(
            "POST",
            f"/v2/users/{state.chat_id}/stream_messages",
            body,
        )
    except Exception as exc:
        if state.stream_msg_id and _is_terminal_stream_lifetime_error(exc):
            # Production evidence shows QQ has already consumed this index:
            # retrying it receives "index needs to increment" and cannot make
            # the expired carrier writable again.  Record the submitted text
            # as visible ownership, then permanently retire this carrier.
            state.last_content = str(body["content_raw"])
            state.next_index += 1
            state.retired = True
            _cancel_stream_expiry(state)
            logger.warning(
                "qqbot-connect-hotfix: QQ C2C stream retired after terminal "
                "lifetime response for chat=%s draft=%s index=%s",
                state.chat_id,
                state.draft_id,
                body["index"],
            )
        elif _is_terminal_passive_reply_budget_error(exc):
            # QQ rejected this request because the inbound message can no
            # longer authorize passive replies. No stream or ordinary retry
            # against the same anchor can succeed, so stop issuing frames.
            state.retired = True
            state.passive_reply_exhausted = True
            _cancel_stream_expiry(state)
            logger.warning(
                "qqbot-connect-hotfix: QQ C2C stream retired after terminal "
                "passive-reply budget response for chat=%s draft=%s index=%s",
                state.chat_id,
                state.draft_id,
                body["index"],
            )
        elif _is_ambiguous_stream_transport_error(exc):
            state.ambiguous_frame = _QQC2CAmbiguousFrame(
                index=int(body["index"]),
                content=str(body["content_raw"]),
                input_state=int(body["input_state"]),
            )
            logger.warning(
                "qqbot-connect-hotfix: QQ C2C stream response ambiguous for "
                "chat=%s draft=%s index=%s; one reconciliation pending",
                state.chat_id,
                state.draft_id,
                body["index"],
            )
        raise
    _record_accepted_stream_frame(adapter, state, body, data)
    return data


def _active_content(
    state: _QQC2CStream,
    content: str,
    *,
    require_committed_prefix: bool,
) -> str:
    """Return the portion belonging to the currently open stream chunk."""

    full = str(content or "")
    committed = str(state.committed_prefix or "")
    if not committed:
        return full
    if full.startswith(committed):
        return full[len(committed):]
    if require_committed_prefix:
        raise RuntimeError(
            "QQ cumulative draft no longer preserves its sealed overflow prefix"
        )
    # Turn-final text can legitimately contain only the final assistant answer
    # while commentary was already streamed. Let _seal_content append that
    # authoritative suffix to the current visible chunk.
    return full


def _visible_stream_content(state: _QQC2CStream) -> str:
    """Return text that QQ has acknowledged as client-visible exactly once."""

    current = str(state.last_content or "") if state.stream_msg_id else ""
    return str(state.committed_prefix or "") + current


def _is_terminal_boundary(character: str) -> bool:
    """Return whether one character separates an independent terminal token."""

    category = unicodedata.category(character)
    return bool(
        character.isspace()
        or (category.startswith("P") and category != "Pc")
    )


def _terminal_payload_is_owned(base: str, payload: str) -> bool:
    """Return whether *payload* has an explicit terminal owner in *base*.

    This is deliberately stricter than substring/overlap matching.  It is used
    both for final composition and for Hermes' ``_interim_send`` callback that
    follows a completed Codex commentary item.  In the latter path the live
    token deltas may already have placed the exact commentary at the end of the
    QQ native stream; sending the callback again would create a second ordinary
    message bubble.
    """

    base = str(base or "")
    payload = str(payload or "")
    if not base or not payload or not base.endswith(payload):
        return False
    boundary = len(base) - len(payload)
    if boundary == 0:
        return True
    if _is_terminal_boundary(payload[0]):
        # The payload already carries its own token boundary. Looking only at
        # the character before the whole suffix would misclassify
        # `progress\nFINAL` + `\nFINAL` as unowned and duplicate the final.
        return True
    return _is_terminal_boundary(base[boundary - 1])


def _append_nonoverlapping(base: str, suffix_source: str) -> str:
    """Compose a cumulative or independent final without value guessing.

    Hermes may provide either the complete cumulative response or a separate
    authoritative final after streamed commentary. Only two observations are
    safe ownership evidence:

    * the final explicitly extends the complete visible body; or
    * the exact final payload is already the terminal, token-bounded body.

    An occurrence elsewhere in commentary, or a coincidental suffix/prefix
    overlap, does not own an independent final and must not swallow it.
    """

    base = str(base or "")
    suffix_source = str(suffix_source or "")
    if not base:
        return suffix_source
    if not suffix_source or suffix_source == base:
        return base
    if suffix_source.startswith(base):
        return suffix_source

    if _terminal_payload_is_owned(base, suffix_source):
        return base

    separator = (
        ""
        if base.endswith(("\n", " ", "\t"))
        or suffix_source.startswith(("\n", " ", "\t"))
        else "\n"
    )
    return base + separator + suffix_source


def _compose_final_content(
    state: _QQC2CStream,
    content: str,
    *,
    proven_delta_content: Optional[str] = None,
) -> str:
    """Build the lossless final text from visible drafts and Hermes' final."""

    visible = _visible_stream_content(state)
    final = str(content or "")
    if (
        proven_delta_content
        and proven_delta_content.startswith(visible)
        and proven_delta_content.endswith(final)
    ):
        # The exact unfinished segment owns the final. The saved pre-finish
        # ledger also covers the tail drained in the same tick as finish(),
        # which may never have been sent as a draft. Never rewrite QQ's prefix.
        return proven_delta_content
    current_visible = visible[len(state.display_prefix):]
    if current_visible and final.startswith(visible):
        # A cumulative final is authoritative and may intentionally omit a
        # deferred draft that never became visible.
        return final
    deferred = str(state.deferred_content or "")
    base = deferred if deferred.startswith(visible) else visible
    if state.display_prefix:
        return state.display_prefix + _append_nonoverlapping(base[len(state.display_prefix):], final)
    return _append_nonoverlapping(base, final)


def _unseen_final_suffix(state: _QQC2CStream, target: str) -> Optional[str]:
    """Return the target suffix not yet visible, or ``None`` on invariant loss."""

    visible = _visible_stream_content(state)
    if not str(target or "").startswith(visible):
        return None
    return str(target or "")[len(visible):]


async def _post_seal_with_retries(
    adapter,
    state: _QQC2CStream,
    content: str,
):
    data = None
    last_error = None
    for attempt, delay in enumerate(_SEAL_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            data = await _post_stream_frame(
                adapter,
                state,
                content,
                input_state=10,
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "qqbot-connect-hotfix: QQ C2C stream seal attempt %s/%s "
                "failed for chat=%s draft=%s index=%s: %s",
                attempt,
                len(_SEAL_RETRY_DELAYS),
                state.chat_id,
                state.draft_id,
                state.next_index,
                exc,
            )
            if state.retired:
                break
    return data, last_error


def _replace_active_stream(adapter, state: _QQC2CStream) -> None:
    streams, anchors = _stream_maps(adapter)
    state_key = _stream_key(state.chat_id, state.draft_id)
    previous = streams.get(state_key)
    if previous is not None and previous is not state:
        _cancel_stream_expiry(previous)
    streams[state_key] = state
    anchors[(state.chat_id, state.reply_to)] = state_key


async def _send_cumulative_draft(adapter, state: _QQC2CStream, content: str):
    """Send a cumulative frame, rolling full chunks into new QQ streams.

    Every stream independently obeys QQ's replace-prefix rule. Once the active
    suffix exceeds the per-message cap, its full head is sealed and recorded
    as ``committed_prefix``; a new stream then owns only the remaining suffix.
    """

    max_length = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000))
    if max_length <= 0:
        raise RuntimeError("QQ native stream message limit must be positive")

    current = state
    data = None
    if (
        current.stream_msg_id
        and current.opened_monotonic is not None
        and _native_stream_now(adapter) - current.opened_monotonic
        >= _native_stream_max_age(adapter)
    ):
        head = str(current.last_content or "")
        data, seal_error = await _post_seal_with_retries(
            adapter,
            current,
            head,
        )
        if seal_error is not None:
            raise seal_error
        current.sealed = True
        committed = current.committed_prefix + head
        completed_id = current.stream_msg_id or current.last_completed_stream_id
        logger.info(
            "qqbot-connect-hotfix: QQ C2C age rollover sealed "
            "draft=%s committed=%s age=%.1fs",
            current.draft_id,
            len(committed),
            _native_stream_now(adapter) - current.opened_monotonic,
        )
        current = _QQC2CStream(
            chat_id=current.chat_id,
            draft_id=current.draft_id,
            reply_to=current.reply_to,
            msg_seq=int(adapter._next_msg_seq(current.reply_to)),
            committed_prefix=committed,
            display_prefix=current.display_prefix,
            last_completed_stream_id=completed_id,
        )
        _replace_active_stream(adapter, current)

    active = _active_content(
        current,
        content,
        require_committed_prefix=True,
    )
    while len(active) > max_length:
        head = active[:max_length]
        if not current.stream_msg_id:
            data = await _post_stream_frame(
                adapter,
                current,
                head,
                input_state=1,
            )
        data, seal_error = await _post_seal_with_retries(
            adapter,
            current,
            head,
        )
        if seal_error is not None:
            raise seal_error

        current.sealed = True
        committed = current.committed_prefix + head
        completed_id = current.stream_msg_id or current.last_completed_stream_id
        logger.info(
            "qqbot-connect-hotfix: QQ C2C overflow chunk sealed "
            "draft=%s committed=%s",
            current.draft_id,
            len(committed),
        )
        current = _QQC2CStream(
            chat_id=current.chat_id,
            draft_id=current.draft_id,
            reply_to=current.reply_to,
            msg_seq=int(adapter._next_msg_seq(current.reply_to)),
            committed_prefix=committed,
            display_prefix=current.display_prefix,
            last_completed_stream_id=completed_id,
        )
        _replace_active_stream(adapter, current)
        active = active[max_length:]

    if active and (not current.stream_msg_id or active != current.last_content):
        data = await _post_stream_frame(
            adapter,
            current,
            active,
            input_state=1,
        )
    return current, data


def _seal_content(adapter, state: _QQC2CStream, content: str) -> tuple[str, str]:
    """Compose one legal seal body and return any overflow separately.

    Hermes' draft contains commentary, tool progress, and often the final
    answer, while its turn-final ``send`` can contain only the short final
    answer. QQ rejects a replace request that removes an already-submitted
    prefix. Never silently discard overflow: callers must roll it into another
    native stream or assign it to an ordinary fallback before reporting
    success.
    """

    previous = str(state.last_content or "")
    max_length = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000))
    composed = _append_nonoverlapping(previous, str(content or ""))
    return composed[:max_length], composed[max_length:]


async def _seal_stream(adapter, state: _QQC2CStream, content: str):
    async with state.lock:
        if state.sealed:
            return _send_result(
                success=True,
                message_id=state.stream_msg_id,
            )
        if state.retired:
            return _send_result(
                success=False,
                error="QQ stream was retired after a terminal QQ response",
            )
        if not state.stream_msg_id:
            return _send_result(
                success=False,
                error="QQ stream cannot be sealed before its first frame",
            )
        seal_content, overflow = _seal_content(adapter, state, content)
        if overflow:
            return _send_result(
                success=False,
                error=(
                    "QQ stream seal requires rollover before closing "
                    f"({len(overflow)} unassigned characters)"
                ),
            )
        data, last_error = await _post_seal_with_retries(
            adapter,
            state,
            seal_content,
        )
        if last_error is not None:
            # Keep both maps intact. A later turn-final retry or
            # abandon_open_draft can still seal this already-visible stream.
            return _send_result(success=False, error=str(last_error))

        state.sealed = True
        logger.info(
            "qqbot-connect-hotfix: QQ C2C stream sealed draft=%s frames=%s",
            state.draft_id,
            state.next_index,
        )
        _remove_stream(adapter, state)
        return _send_result(
            success=True,
            message_id=state.stream_msg_id,
            raw_response=data,
        )


async def _complete_retired_stream_final(
    adapter,
    original_send,
    state: _QQC2CStream,
    *,
    target: str,
    final_payload: str,
    chat_id: str,
    reply_to: Optional[str],
    metadata: Optional[Dict[str, Any]],
):
    """Deliver only text not owned by a deliberately retired QQ carrier."""

    if state.passive_reply_exhausted:
        # The platform rejected the inbound anchor itself, not just this
        # carrier. Preserve the state for a later Gateway recovery path, but
        # do not issue an ordinary send that QQ has already declared invalid.
        return _send_result(
            success=False,
            error="QQ passive-reply window or budget is exhausted",
        )

    unseen = _unseen_final_suffix(state, target)
    if unseen is None:
        return _send_result(
            success=False,
            error="QQ retired stream final no longer extends visible content",
        )
    if unseen:
        result = await original_send(
            adapter,
            chat_id,
            unseen,
            reply_to=reply_to,
            metadata=metadata,
        )
        if not getattr(result, "success", False):
            return result
    else:
        result = _send_result(
            success=True,
            message_id=state.stream_msg_id,
            raw_response={"qq_retired_stream_owned": True},
        )
    _remember_turn_tombstone(
        adapter,
        state,
        final_payload=final_payload,
        final_content=target,
    )
    _remove_stream(adapter, state)
    return result


async def _complete_turn_final(
    adapter,
    original_send,
    *,
    chat_id: str,
    content: str,
    reply_to: Optional[str],
    metadata: Optional[Dict[str, Any]],
    anchor: str,
):
    """Run one complete QQ final-ownership transaction.

    Every active, pending, cancelled and replay lookup happens inside the
    caller's keyed broker flight.  A successful return therefore means the
    native/ordinary delivery and its ownership promotion completed before the
    result becomes visible to same-key waiters.
    """

    streams, anchors = _stream_maps(adapter)
    state_key = anchors.get((str(chat_id), anchor))
    state = streams.get(state_key) if state_key is not None else None
    if state is None:
        completed_owner = _completed_owner_for_final(
            adapter,
            chat_id=str(chat_id),
            reply_to=anchor,
            content=str(content or ""),
        )
        if completed_owner is None:
            transient_owner = _final_delivery_broker(
                adapter
            ).transient_completion_for((str(chat_id), anchor))
            if (
                transient_owner is not None
                and _turn_tombstone_owns_final(
                    transient_owner,
                    chat_id=str(chat_id),
                    reply_to=anchor,
                    payload=str(content or ""),
                )
            ):
                completed_owner = transient_owner
        if completed_owner is not None:
            return _send_result(
                success=True,
                message_id=completed_owner.message_id,
                raw_response={"qq_completed_turn_owned": True},
            )
        cancelled_owner = _cancelled_owner_for_anchor(
            adapter,
            chat_id=str(chat_id),
            reply_to=anchor,
        )
        if cancelled_owner is not None:
            normal_result = await original_send(
                adapter,
                chat_id,
                content,
                reply_to=reply_to,
                metadata=metadata,
            )
            if getattr(normal_result, "success", False):
                _promote_cancelled_owner(
                    adapter,
                    cancelled_owner,
                    final_content=str(content or ""),
                    message_id=getattr(normal_result, "message_id", None),
                )
            return normal_result
        pending_state = _final_only_pending_for_anchor(
            adapter,
            chat_id=str(chat_id),
            reply_to=anchor,
        )
        if pending_state is not None:
            normal_result = await original_send(
                adapter,
                chat_id,
                content,
                reply_to=reply_to,
                metadata=metadata,
            )
            if getattr(normal_result, "success", False):
                _remember_turn_tombstone(
                    adapter,
                    pending_state,
                    final_payload=str(content or ""),
                    final_content=str(content or ""),
                )
                _remove_final_only_pending(adapter, pending_state)
            return normal_result

        # No native lifecycle exists for this turn. It still participates in
        # the broker so concurrent notify=True callbacks share one result.
        return await original_send(
            adapter,
            chat_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
        )

    final_context = _TURN_FINAL_CONTEXT.get()
    proven_delta_content = None
    if (
        isinstance(final_context, _TurnFinalContext)
        and final_context.adapter_identity == id(adapter)
        and final_context.chat_id == str(chat_id)
        and final_context.anchor == anchor
        and bool(final_context.delta_payload)
        and str(content or "").startswith(final_context.delta_payload)
        and final_context.delta_content.endswith(final_context.delta_payload)
    ):
        # A post-stream footer may extend the exact unfinished final segment.
        # Keep its ledger prefix and replace only that proven segment, never
        # infer a segment boundary from the visible text's suffix alone.
        proven_delta_content = (
            final_context.delta_content[:-len(final_context.delta_payload)]
            + str(content or "")
        )

    if state.retired:
        if state.ordinary_owned_suffix:
            # A fallback suffix was already delivered before a terminal
            # lifetime response rejected the cleanup seal. Both carriers are
            # immutable, so publish ownership and remove the dead lifecycle.
            final_content = _ordinary_owned_final_content(state)
            _remember_turn_tombstone(
                adapter,
                state,
                final_payload=str(content or ""),
                final_content=final_content,
            )
            _remove_stream(adapter, state)
            return _send_result(
                success=True,
                message_id=state.stream_msg_id,
                raw_response={"qq_retired_stream_owned": True},
            )
        return await _complete_retired_stream_final(
            adapter,
            original_send,
            state,
            target=_compose_final_content(
                state,
                content,
                proven_delta_content=proven_delta_content,
            ),
            final_payload=str(content or ""),
            chat_id=str(chat_id),
            reply_to=reply_to,
            metadata=metadata,
        )

    if state.ordinary_owned_suffix:
        # A prior successful final fallback is authoritative and immutable.
        # Retried final callbacks may only close the still-open native prefix.
        closed = await _seal_stream(adapter, state, state.last_content)
        if closed.success:
            _remember_turn_tombstone(
                adapter,
                state,
                final_payload=str(content or ""),
                final_content=_ordinary_owned_final_content(state),
            )
            return closed
        return _QQC2CFinalAttemptOutcome(
            _send_result(
                success=True,
                message_id=state.stream_msg_id,
                raw_response={"qq_stream_close_pending": True},
            ),
            cache_completed=False,
        )

    if not state.stream_msg_id and not state.committed_prefix:
        # No native frame ever became visible: preserve exactly one ordinary
        # final rather than opening a new native lifecycle at completion.
        normal_result = await original_send(
            adapter,
            chat_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
        )
        if getattr(normal_result, "success", False):
            _remember_turn_tombstone(
                adapter,
                state,
                final_payload=str(content or ""),
                final_content=str(content or ""),
            )
            _remove_stream(adapter, state)
        return normal_result

    # Hermes can finish with either the whole cumulative response or a short
    # final-only answer. Compose against the exact QQ-acknowledged prefix, then
    # apply one rollover path.
    target = _compose_final_content(
        state,
        content,
        proven_delta_content=proven_delta_content,
    )
    rollover_error = None
    try:
        async with state.lock:
            state, _data = await _send_cumulative_draft(adapter, state, target)
    except Exception as exc:
        rollover_error = exc
        logger.warning(
            "qqbot-connect-hotfix: final rollover stopped for chat=%s "
            "draft=%s: %s",
            chat_id,
            state.draft_id,
            exc,
        )
        # Rollover can replace the active state before a later operation fails.
        latest_streams, _latest_anchors = _stream_maps(adapter)
        latest_state = latest_streams.get(state_key)
        if latest_state is not None:
            state = latest_state

    if state.retired:
        return await _complete_retired_stream_final(
            adapter,
            original_send,
            state,
            target=target,
            final_payload=str(content or ""),
            chat_id=str(chat_id),
            reply_to=reply_to,
            metadata=metadata,
        )

    unseen = _unseen_final_suffix(state, target)
    if unseen is None:
        logger.error(
            "qqbot-connect-hotfix: final ownership invariant lost for "
            "chat=%s draft=%s; refusing duplicate fallback",
            chat_id,
            state.draft_id,
        )
        return _send_result(
            success=False,
            error="QQ stream final no longer extends visible content",
        )

    if unseen:
        # This external send and its ownership promotion remain inside the same
        # keyed broker flight as the failed native update. A concurrent final
        # cannot calculate or send the same suffix before promotion completes.
        normal_result = await original_send(
            adapter,
            chat_id,
            unseen,
            reply_to=reply_to,
            metadata=metadata,
        )
        cache_completed = True
        if getattr(normal_result, "success", False):
            _remember_turn_tombstone(
                adapter,
                state,
                final_payload=str(content or ""),
                final_content=target,
            )
            if state.stream_msg_id:
                state.ordinary_owned_suffix = unseen
                recovery = await _seal_stream(adapter, state, state.last_content)
                if state.retired:
                    _remove_stream(adapter, state)
                elif not recovery.success:
                    cache_completed = False
                    logger.warning(
                        "qqbot-connect-hotfix: suffix fallback sent but "
                        "visible stream close remains pending for chat=%s "
                        "draft=%s: %s",
                        chat_id,
                        state.draft_id,
                        recovery.error,
                    )
            else:
                _remove_stream(adapter, state)
        return _QQC2CFinalAttemptOutcome(
            normal_result,
            cache_completed=cache_completed,
        )

    if not state.stream_msg_id and state.committed_prefix == target:
        completed_id = state.last_completed_stream_id
        _remember_turn_tombstone(
            adapter,
            state,
            final_payload=str(content or ""),
            final_content=target,
        )
        _remove_stream(adapter, state)
        return _send_result(success=True, message_id=completed_id)

    sealed = await _seal_stream(adapter, state, state.last_content)
    if sealed.success:
        _remember_turn_tombstone(
            adapter,
            state,
            final_payload=str(content or ""),
            final_content=target,
        )
        return sealed

    # The complete target is already visible. Retry closing the same body but
    # never emit an ordinary duplicate. If both close rounds fail, retain the
    # state for abandon/retry while reporting visible delivery success.
    recovery = await _seal_stream(adapter, state, state.last_content)
    if recovery.success:
        _remember_turn_tombstone(
            adapter,
            state,
            final_payload=str(content or ""),
            final_content=target,
        )
        return recovery
    logger.warning(
        "qqbot-connect-hotfix: final is visible but stream close remains "
        "pending for chat=%s draft=%s after rollover=%s: %s",
        chat_id,
        state.draft_id,
        bool(rollover_error),
        recovery.error,
    )
    state.close_pending_final_payload = str(content or "")
    state.close_pending_final_content = target
    return _QQC2CFinalAttemptOutcome(
        _send_result(
            success=True,
            message_id=state.stream_msg_id,
            raw_response={"qq_stream_close_pending": True},
        ),
        cache_completed=False,
    )


def patch_qq_c2c_streaming(QQAdapter):
    """Add official QQ C2C native streaming to ``QQAdapter``.

    The patch composes with Hermes' ``GatewayStreamConsumer``:

    * ``send_draft`` opens/replaces the QQ stream;
    * ``draft_stream_is_message`` keeps one stream across tool boundaries;
    * the turn-final ``send`` seals it with ``input_state=10``;
    * cancellation uses ``abandon_open_draft`` to close the visible stream;
    * QQ's passive ``input_notify`` is emitted at most once per inbound
      ``msg_id`` so a fallback path still has reply budget for the final.
    """

    if not _hermes_streaming_supported():
        found = _hermes_version_tuple()
        found_text = ".".join(str(part) for part in found) or "unknown"
        return (
            "QQ C2C native streaming disabled: requires Hermes >=0.20.5 "
            f"(found {found_text})"
        )

    original_send = QQAdapter.send
    if getattr(original_send, _STREAM_PATCHED, False):
        return "QQ C2C native streaming already patched"

    original_send_typing = QQAdapter.send_typing

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        del metadata
        return bool(chat_id) and str(chat_id) in _native_lane_chats(self) and _is_c2c(
            self,
            str(chat_id),
            chat_type,
        )

    def stream_is_message_for_chat(self, chat_id: str) -> bool:
        return _is_c2c(self, str(chat_id))

    async def _send_draft_uncoordinated(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        chat_id = str(chat_id)
        if not _is_c2c(self, chat_id):
            return _send_result(
                success=False,
                error="QQ native streaming is supported only for C2C chats",
            )

        reply_to = _reply_anchor(metadata)
        if not reply_to:
            reply_to = str(getattr(self, "_last_msg_id", {}).get(chat_id) or "")
        if not reply_to:
            return _send_result(
                success=False,
                error="QQ native streaming requires the inbound reply msg_id",
            )

        streams, anchors = _stream_maps(self)
        state_key = _stream_key(chat_id, int(draft_id))
        state = streams.get(state_key)
        if state is None:
            anchored_key = anchors.get((chat_id, reply_to))
            anchored_state = (
                streams.get(anchored_key)
                if anchored_key is not None
                else None
            )
            if (
                anchored_state is not None
                and (
                    anchored_state.ordinary_owned_suffix
                    or anchored_state.close_pending_final_content is not None
                )
            ):
                # A completed visible turn is anchored by the inbound QQ
                # message, not by Hermes' draft counter. A stale consumer may
                # arrive with a different draft id; do not create a second
                # stream or overwrite the anchor that can still seal the first.
                return _send_result(
                    success=True,
                    message_id=anchored_state.stream_msg_id,
                    raw_response={"qq_visible_final_owned": True},
                )
            transient_owner = _final_delivery_broker(
                self
            ).transient_completion_for((chat_id, reply_to))
            if transient_owner is not None:
                return _send_result(
                    success=True,
                    message_id=transient_owner.message_id,
                    raw_response={"qq_transient_anchor_owned": True},
                )
            completed_delivery = _final_delivery_broker(self).completed_for(
                (chat_id, reply_to)
            )
            if completed_delivery is not None:
                return _send_result(
                    success=True,
                    message_id=getattr(
                        completed_delivery,
                        "message_id",
                        None,
                    ),
                    raw_response={"qq_completed_anchor_owned": True},
                )
            completed_owner = _completed_owner_for_draft(
                self,
                chat_id=chat_id,
                reply_to=reply_to,
                draft_id=int(draft_id),
            )
            if completed_owner is not None:
                return _send_result(
                    success=True,
                    message_id=completed_owner.message_id,
                    raw_response={"qq_completed_turn_owned": True},
                )
            pending_state = _final_only_pending_for_draft(
                self,
                chat_id=chat_id,
                reply_to=reply_to,
                draft_id=int(draft_id),
            )
            if pending_state is not None:
                return _send_result(
                    success=True,
                    raw_response={"qq_final_only_pending": True},
                )
            _evict_unopened_streams(self, limit=_MAX_OPEN_STREAMS - 1)
            if len(streams) >= _MAX_OPEN_STREAMS:
                logger.warning(
                    "qqbot-connect-hotfix: native C2C stream capacity reached; "
                    "keeping %s opened streams retryable and using final-only "
                    "delivery for chat=%s",
                    len(streams),
                    chat_id,
                )
                # Reporting success keeps GatewayStreamConsumer on the native
                # lane without emitting an uneditable partial. Since no anchor
                # is registered, retain a separate bounded identity so the
                # turn-final wrapper can own one normal final and reject stale
                # retries/late draft frames after completion.
                _remember_final_only_pending(
                    self,
                    _QQC2CStream(
                        chat_id=chat_id,
                        draft_id=int(draft_id),
                        reply_to=reply_to,
                        msg_seq=int(self._next_msg_seq(reply_to)),
                    ),
                )
                return _send_result(success=True)
            state = _QQC2CStream(
                chat_id=chat_id,
                draft_id=int(draft_id),
                reply_to=reply_to,
                msg_seq=int(self._next_msg_seq(reply_to)),
            )
            streams[state_key] = state
            anchors[(chat_id, reply_to)] = state_key
            _mark_native_lane(self, chat_id)
        elif state.reply_to != reply_to:
            return _send_result(
                success=False,
                error="QQ native stream draft identity changed mid-turn",
            )

        async with state.lock:
            if state.sealed:
                return _send_result(
                    success=False,
                    error="QQ native stream is already sealed",
                )
            if state.retired:
                return _send_result(
                    success=True,
                    message_id=state.stream_msg_id,
                    raw_response={"qq_retired_stream_owned": True},
                )
            if (
                state.ordinary_owned_suffix
                or state.close_pending_final_content is not None
            ):
                # The turn already has a complete visible final, either across
                # an immutable ordinary suffix or in the still-open native
                # stream. A late frame from the old consumer cannot safely
                # move or extend that final inside the replace lifecycle.
                return _send_result(
                    success=True,
                    message_id=state.stream_msg_id,
                    raw_response={"qq_visible_final_owned": True},
                )
            if state.frame_retry_not_before > _native_stream_now(self):
                state.deferred_content = str(content or "")
                return _send_result(
                    success=True,
                    message_id=state.stream_msg_id,
                    raw_response={"qq_stream_frame_coalesced": True},
                )
            # Until this cumulative body is acknowledged, retain it as the
            # lossless final fallback source. This also preserves the newest
            # body if an ambiguous reconciliation retires the carrier.
            state.deferred_content = str(content or "")
            try:
                state, data = await _send_cumulative_draft(
                    self,
                    state,
                    content,
                )
            except Exception as exc:
                # A rollover installs the replacement carrier before its
                # first frame is submitted. If that submission receives a
                # terminal response, the local variable still references the
                # sealed predecessor; resolve the authoritative carrier before
                # deciding whether this frame may be retried.
                latest_streams, _latest_anchors = _stream_maps(self)
                latest_state = latest_streams.get(state_key)
                if latest_state is not None:
                    state = latest_state
                if state.retired:
                    logger.warning(
                        "qqbot-connect-hotfix: QQ C2C stream carrier retired "
                        "for chat=%s draft=%s; awaiting final suffix: %s",
                        chat_id,
                        draft_id,
                        exc,
                    )
                else:
                    _defer_stream_frame(self, state, content)
                    logger.warning(
                        "qqbot-connect-hotfix: QQ C2C stream frame deferred for "
                        "chat=%s draft=%s index=%s; retaining final-only "
                        "fallback: %s",
                        chat_id,
                        draft_id,
                        state.next_index,
                        exc,
                    )
                # QQ ordinary messages cannot be edited. Reporting failure to
                # GatewayStreamConsumer would make its generic fallback send a
                # partial message immediately, then another final. Keep the
                # native lane selected: a later frame can retry the same index;
                # if no frame ever opens, the final send wrapper falls back to
                # exactly one ordinary message.
                return _send_result(success=True)

            _clear_stream_frame_cooldown(state)

        return _send_result(
            success=True,
            raw_response=data,
        )

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Coordinate every stable-anchor frame with its final lifecycle.

        The final broker owns the interval through external ordinary delivery
        and terminal-marker publication. Joining that same key prevents a
        stale frame (including the original draft id) from reopening or
        replacing a carrier inside that interval. Different reply anchors use
        distinct claims and remain independent.
        """

        chat_key = str(chat_id)
        reply_to = _reply_anchor(metadata)
        if not reply_to:
            reply_to = str(
                getattr(self, "_last_msg_id", {}).get(chat_key) or ""
            )
        resolved_metadata = metadata
        if reply_to and not _reply_anchor(metadata):
            # Freeze the fallback identity before waiting on a same-anchor
            # final. `_last_msg_id` can advance to a newer inbound message
            # while this callback is queued; recomputing it afterward would
            # coordinate on one key and mutate another turn's stream.
            resolved_metadata = dict(metadata or {})
            resolved_metadata["reply_to_message_id"] = reply_to

        async def send_once():
            return await _send_draft_uncoordinated(
                self,
                chat_key,
                draft_id,
                content,
                resolved_metadata,
            )

        if not reply_to or not _is_c2c(self, chat_key):
            return await send_once()
        return await _final_delivery_broker(self).coordinate(
            (chat_key, reply_to),
            send_once,
        )

    async def abandon_open_draft(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        anchor = _reply_anchor(metadata)
        chat_key = str(chat_id)

        async def abandon_once():
            streams, anchors = _stream_maps(self)
            state_key = anchors.get((chat_key, anchor))
            state = streams.get(state_key) if state_key is not None else None
            if state is None:
                pending_state = _final_only_pending_for_anchor(
                    self,
                    chat_id=chat_key,
                    reply_to=anchor,
                )
                if pending_state is not None:
                    _remember_turn_tombstone(
                        self,
                        pending_state,
                        final_payload=str(content or ""),
                        final_content=str(content or ""),
                        final_delivered=False,
                    )
                    _remove_final_only_pending(self, pending_state)
                return _send_result(success=True)
            if state.retired:
                owner = _remember_turn_tombstone(
                    self,
                    state,
                    final_payload=str(content or ""),
                    final_content=_visible_stream_content(state),
                    final_delivered=False,
                )
                _remember_transient_turn_completion(self, owner)
                _remove_stream(self, state)
                return _send_result(
                    success=True,
                    message_id=state.stream_msg_id,
                    raw_response={"qq_retired_stream_abandoned": True},
                )
            if state.ordinary_owned_suffix:
                # A completed ordinary fallback already owns every character
                # after the native stream's last acknowledged frame. Delayed
                # cancellation cleanup must close only that native frame.
                closed = await _seal_stream(
                    self,
                    state,
                    state.last_content,
                )
                if closed.success:
                    _publish_external_turn_completion(
                        self,
                        state,
                        closed,
                        final_payload=str(content or ""),
                        final_content=_ordinary_owned_final_content(state),
                    )
                return closed
            active_content = _active_content(
                state,
                content or state.last_content,
                require_committed_prefix=False,
            )
            if (
                not state.stream_msg_id
                and state.committed_prefix
                and not active_content
            ):
                completed_id = state.last_completed_stream_id
                completed = _send_result(
                    success=True,
                    message_id=completed_id,
                )
                owner = _remember_turn_tombstone(
                    self,
                    state,
                    final_payload=str(content or ""),
                    final_content=state.committed_prefix,
                )
                _remember_transient_turn_completion(self, owner)
                _remove_stream(self, state)
                return completed
            sealed = await _seal_stream(
                self,
                state,
                active_content or state.last_content,
            )
            if sealed.success:
                if state.close_pending_final_content is not None:
                    _publish_external_turn_completion(
                        self,
                        state,
                        sealed,
                        final_payload=str(
                            state.close_pending_final_payload or ""
                        ),
                        final_content=state.close_pending_final_content,
                    )
                else:
                    owner = _remember_turn_tombstone(
                        self,
                        state,
                        final_payload=str(content or ""),
                        final_content=_visible_stream_content(state),
                    )
                    _remember_transient_turn_completion(self, owner)
            return sealed

        if not anchor:
            return await abandon_once()
        return await _final_delivery_broker(self).coordinate(
            (chat_key, anchor),
            abandon_once,
        )

    @functools.wraps(original_send)
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        # Hermes emits a completed Codex commentary item through an ordinary
        # ``_interim_send`` callback even when its live deltas already rendered
        # the same item in this turn's native QQ stream. Suppress only when the
        # same inbound reply anchor still owns an open stream and the callback
        # payload is exactly its token-bounded terminal text. Earlier/nonterminal
        # occurrences, another anchor, unopened streams, and every other send
        # continue through the ordinary QQ path.
        if (
            isinstance(metadata, dict)
            and metadata.get("_interim_send") is True
            and _is_c2c(self, str(chat_id))
        ):
            commentary_context = _COMPLETED_COMMENTARY_CONTEXT.get()
            trusted_commentary = bool(
                isinstance(commentary_context, _CompletedCommentaryContext)
                and commentary_context.adapter_identity == id(self)
                and commentary_context.chat_id == str(chat_id)
                and commentary_context.cleaned == str(content or "")
            )
            anchor = str(
                reply_to
                or metadata.get("reply_to_message_id")
                or (commentary_context.anchor if trusted_commentary else "")
                or ""
            ).strip()
            streams, anchors = _stream_maps(self)
            state_key = anchors.get((str(chat_id), anchor))
            state = streams.get(state_key) if state_key is not None else None
            if state is None and not anchor:
                # GatewayStreamConsumer._send_commentary currently omits its
                # initial_reply_to_id from metadata. Recover only when content
                # ownership identifies exactly one open stream in this C2C
                # chat. Multiple matching concurrent turns remain ambiguous
                # and deliberately fall through to the ordinary send.
                candidates = [
                    candidate
                    for candidate in streams.values()
                    if (
                        candidate.chat_id == str(chat_id)
                        and candidate.stream_msg_id
                        and not candidate.sealed
                        and _terminal_payload_is_owned(
                            _visible_stream_content(candidate),
                            str(content or ""),
                        )
                    )
                ]
                if len(candidates) == 1:
                    state = candidates[0]
            if (
                state is not None
                and state.stream_msg_id
                and not state.sealed
                and (
                    _terminal_payload_is_owned(
                        _visible_stream_content(state),
                        str(content or ""),
                    )
                    or (
                        trusted_commentary
                        and _visible_stream_content(state).endswith(
                            str(content or "")
                        )
                    )
                )
            ):
                logger.debug(
                    "qqbot-connect-hotfix: suppressed already-streamed QQ "
                    "C2C interim carrier draft=%s",
                    state.draft_id,
                )
                return _send_result(
                    success=True,
                    message_id=state.stream_msg_id,
                    raw_response={"qq_stream_owned_interim": True},
                )

        # GatewayStreamConsumer marks its turn-final send with notify=True.
        # Only intercept that exact path: approvals, slash-command replies,
        # heartbeats, and steering acknowledgements must remain independent.
        if (
            isinstance(metadata, dict)
            and metadata.get("notify") is True
            and _is_c2c(self, str(chat_id))
        ):
            anchor = str(
                reply_to
                or metadata.get("reply_to_message_id")
                or ""
            ).strip()

            async def complete_once():
                return await _complete_turn_final(
                    self,
                    original_send,
                    chat_id=str(chat_id),
                    content=str(content or ""),
                    reply_to=reply_to,
                    metadata=metadata,
                    anchor=anchor,
                )

            if not anchor:
                # Without a stable inbound identity, `(chat_id, "")` would
                # merge unrelated turns and replay the first final for every
                # later unanchored completion in this private chat.
                return await complete_once()
            return await _final_delivery_broker(self).run(
                (str(chat_id), anchor),
                complete_once,
            )
        return await original_send(
            self,
            chat_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
        )

    @functools.wraps(original_send_typing)
    async def send_typing(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        chat_id = str(chat_id)
        if not _typing_budget_applies(self, chat_id):
            return await original_send_typing(self, chat_id, metadata=metadata)
        msg_id = str(getattr(self, "_last_msg_id", {}).get(chat_id) or "")
        if not msg_id:
            return await original_send_typing(self, chat_id, metadata=metadata)

        seen = getattr(self, "_qq_stream_typing_anchors", None)
        if seen is None:
            seen = {}
            self._qq_stream_typing_anchors = seen
        key = (chat_id, msg_id)
        if key in seen:
            return None
        seen[key] = None
        while len(seen) > _MAX_TYPING_ANCHORS:
            seen.pop(next(iter(seen)))
        return await original_send_typing(self, chat_id, metadata=metadata)

    setattr(send, _STREAM_PATCHED, True)
    setattr(send_draft, _STREAM_PATCHED, True)
    setattr(send_typing, _STREAM_PATCHED, True)
    QQAdapter.supports_draft_streaming = supports_draft_streaming
    QQAdapter.stream_is_message_for_chat = stream_is_message_for_chat
    QQAdapter.draft_stream_is_message = True
    QQAdapter.send_draft = send_draft
    QQAdapter.abandon_open_draft = abandon_open_draft
    QQAdapter.send = send
    QQAdapter.send_typing = send_typing
    gate_status = _patch_gateway_stream_gate(QQAdapter)
    overflow_status = _patch_gateway_overflow_limit(QQAdapter)
    commentary_status = _patch_gateway_commentary_context(QQAdapter)
    final_context_status = _patch_gateway_turn_final_context(QQAdapter)
    from .steering import patch_qq_steering
    steer_status = patch_qq_steering(QQAdapter)
    logger.info("qqbot-connect-hotfix: %s", gate_status)
    logger.info("qqbot-connect-hotfix: %s", overflow_status)
    logger.info("qqbot-connect-hotfix: %s", commentary_status)
    logger.info("qqbot-connect-hotfix: %s", final_context_status)
    logger.info("qqbot-connect-hotfix: %s", steer_status)
    return "QQ C2C native streaming patched"
