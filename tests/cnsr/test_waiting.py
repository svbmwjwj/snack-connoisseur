from cnsr.waiting import Deadline


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_deadline_counts_every_second_on_its_clock():
    clock = Clock()
    deadline = Deadline(30, clock)
    assert not deadline.passed() and deadline.left() == 30
    clock.now += 29.5  # an attempt that took most of the time
    assert not deadline.passed() and deadline.left() == 0.5
    clock.now += 0.5
    assert deadline.passed() and deadline.left() == 0
    assert deadline.seconds == 30


def test_a_step_never_goes_past_the_deadline():
    clock = Clock()
    deadline = Deadline(25, clock)
    assert deadline.step(10) == 10
    clock.now += 20
    assert deadline.step(10) == 5
