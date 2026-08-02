"""Single-flight coalescing of one-shot external-camera captures (#2705-shape
fix, filed against the external-camera path as a follow-up on #2707).

V4L2 USB devices allow exactly one open handle - the same one-connection
limit #2705 covers for Bambu firmware. The #2707 guards (``is_stream_active``
/ ``try_get_active_buffered_frame``) only keep a one-shot capturer from
competing with the fan-out live view; nothing kept the capturers from
competing with EACH OTHER when no viewer is attached, so an Obico poll and
the in-print frame bank (say) could each open their own connection to the
same USB device and collide.

These tests drive ``capture_frame`` at the public boundary and count how
many times the underlying capture ran, since "how many connections did we
open" is the entire point of the fix. Mirrors
``test_camera_capture_coalescing.py``'s structure for the built-in path.
"""

import asyncio

import pytest

from backend.app.services import external_camera as ec_module
from backend.app.services.external_camera import capture_frame, capture_in_flight

FRAME_A = b"\xff\xd8" + b"a" * 200 + b"\xff\xd9"
FRAME_B = b"\xff\xd8" + b"b" * 200 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def _clear_inflight():
    """The registry is module-global; don't leak tasks between tests."""
    ec_module._inflight_captures.clear()
    yield
    ec_module._inflight_captures.clear()


class RecordingCapture:
    """Stand-in for the real capture, recording each call.

    ``gate`` (when set) holds every capture open until released, which is how
    these tests create the overlap window that used to produce two
    connections.
    """

    def __init__(self, frames=(FRAME_A, FRAME_B), gate: asyncio.Event | None = None):
        self.calls: list[tuple[str, str, str | None, int]] = []
        self._frames = list(frames)
        self._gate = gate
        self.started = asyncio.Event()

    async def __call__(self, url, camera_type, timeout, snapshot_url):
        self.calls.append((url, camera_type, snapshot_url, timeout))
        self.started.set()
        if self._gate is not None:
            await self._gate.wait()
        return self._frames.pop(0) if self._frames else None

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def patch_capture(monkeypatch):
    def _install(capture):
        monkeypatch.setattr(ec_module, "_capture_frame_uncoalesced", capture)
        return capture

    return _install


async def _let_leader_start(capture: RecordingCapture) -> None:
    """Wait until the leader is inside the capture, so the next caller joins it.

    Without this the second caller can reach the registry before the first
    has even been scheduled, which tests a different (and uninteresting) race.
    """
    await asyncio.wait_for(capture.started.wait(), timeout=1)


@pytest.mark.asyncio
async def test_simultaneous_callers_share_one_capture(patch_capture):
    """The reported collision: two consumers, one connection, two frames."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=20))
    await _let_leader_start(capture)
    follower = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=15))
    await asyncio.sleep(0)
    gate.set()

    assert await leader == FRAME_A
    assert await follower == FRAME_A
    assert capture.count == 1


@pytest.mark.asyncio
async def test_five_callers_one_capture(patch_capture):
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    first = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)
    rest = [asyncio.create_task(capture_frame("/dev/video1", "usb")) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()

    assert await asyncio.gather(first, *rest) == [FRAME_A] * 5
    assert capture.count == 1


@pytest.mark.asyncio
async def test_different_cameras_do_not_coalesce(patch_capture):
    """The one-connection limit is per camera, so the key must be too."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    one = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)
    two = asyncio.create_task(capture_frame("/dev/video2", "usb"))
    await asyncio.sleep(0)
    gate.set()

    assert {await one, await two} == {FRAME_A, FRAME_B}
    assert capture.count == 2
    assert {url for url, *_ in capture.calls} == {"/dev/video1", "/dev/video2"}


@pytest.mark.asyncio
async def test_different_snapshot_url_does_not_coalesce(patch_capture):
    """#1177's snapshot_url override routes to a different endpoint entirely -
    two printers sharing a camera_url but differing only in snapshot_url must
    not share a capture."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    one = asyncio.create_task(capture_frame("http://cam/", "mjpeg", snapshot_url="http://cam/frame1.jpg"))
    await _let_leader_start(capture)
    two = asyncio.create_task(capture_frame("http://cam/", "mjpeg", snapshot_url="http://cam/frame2.jpg"))
    await asyncio.sleep(0)
    gate.set()

    assert {await one, await two} == {FRAME_A, FRAME_B}
    assert capture.count == 2


@pytest.mark.asyncio
async def test_coalescing_is_not_caching(patch_capture):
    """Sequential callers each capture fresh.

    Deliberate: plate detection and the finish-photo path decide things about
    a running print from these frames, and #1397 was a finish photo a few
    seconds stale showing the bed already lowered.
    """
    capture = patch_capture(RecordingCapture())

    assert await capture_frame("/dev/video1", "usb") == FRAME_A
    assert await capture_frame("/dev/video1", "usb") == FRAME_B
    assert capture.count == 2


@pytest.mark.asyncio
async def test_registry_is_empty_after_a_capture_finishes(patch_capture):
    """No leak, and nothing left behind for the next caller to join."""
    patch_capture(RecordingCapture())

    await capture_frame("/dev/video1", "usb")
    await asyncio.sleep(0)  # let the done-callback run

    assert ec_module._inflight_captures == {}
    assert capture_in_flight("/dev/video1", "usb") is False


@pytest.mark.asyncio
async def test_failed_leader_does_not_poison_its_followers(patch_capture):
    """A follower that never got its own attempt gets one when the leader fails.

    Safe by then: the leader has finished, so there is no connection to
    compete with. This also covers the follower whose timeout is LONGER than
    the leader's — it isn't cut short by someone else's deadline.
    """
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(frames=(None, FRAME_B), gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=10))
    await _let_leader_start(capture)
    follower = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=20))
    await asyncio.sleep(0)
    gate.set()

    assert await leader is None
    assert await follower == FRAME_B
    assert capture.count == 2


@pytest.mark.asyncio
async def test_two_consecutive_failures_give_up(patch_capture):
    """Bounded retry: a follower doesn't chase failing captures forever.

    Two followers behind a failing leader. The first takes its own turn, the
    second joins THAT capture, and when it fails too the second gives up
    rather than opening a third connection.
    """
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(frames=(None, None), gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)
    first = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await asyncio.sleep(0)
    second = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await asyncio.sleep(0)
    gate.set()

    assert await leader is None
    assert await first is None
    assert await second is None
    # The leader's capture plus one retry — not one per disappointed caller.
    assert capture.count == 2


@pytest.mark.asyncio
async def test_follower_timeout_does_not_sabotage_the_capture(patch_capture):
    """A follower giving up leaves the capture running for everyone else.

    Call sites disagree about the timeout, so a follower must be able to
    abandon a join without cancelling a capture other callers are still
    waiting on.
    """
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=30))
    await _let_leader_start(capture)
    impatient = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=0.01))
    patient = asyncio.create_task(capture_frame("/dev/video1", "usb", timeout=30))

    assert await impatient is None  # gave up on its own deadline
    gate.set()

    assert await leader == FRAME_A
    assert await patient == FRAME_A  # unaffected by the one that walked away
    assert capture.count == 1


@pytest.mark.asyncio
async def test_cancelled_leader_still_delivers_to_followers(patch_capture):
    """Snapshot/capture requests get cancelled routinely (client navigates
    away mid-request). The follower must not lose the frame because the
    caller that happened to open the connection went away."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)
    follower = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await asyncio.sleep(0)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    gate.set()

    assert await follower == FRAME_A
    assert capture.count == 1


@pytest.mark.asyncio
async def test_cancelling_a_follower_leaves_the_leader_alone(patch_capture):
    """The mirror case: the follower's cancellation is its own business."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)
    follower = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await asyncio.sleep(0)

    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    gate.set()

    assert await leader == FRAME_A
    assert capture.count == 1


@pytest.mark.asyncio
async def test_capture_in_flight_reports_the_window(patch_capture):
    """The predicate a diagnose-style caller would use to know it will join,
    not measure its own connection."""
    gate = asyncio.Event()
    capture = patch_capture(RecordingCapture(gate=gate))

    assert capture_in_flight("/dev/video1", "usb") is False

    leader = asyncio.create_task(capture_frame("/dev/video1", "usb"))
    await _let_leader_start(capture)

    assert capture_in_flight("/dev/video1", "usb") is True
    assert capture_in_flight("/dev/video2", "usb") is False  # per camera

    gate.set()
    await leader
    await asyncio.sleep(0)

    assert capture_in_flight("/dev/video1", "usb") is False
