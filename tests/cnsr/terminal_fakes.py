"""The terminal double for ``Ui``: typed answers in order, and what was shown and logged."""

from __future__ import annotations

import io
import logging

from cnsr.progress import StageDisplay
from cnsr.runlog import LOGGER_NAME
from cnsr.ui import Ui


class Terminal:
    """Typed answers for ``Ui``, in order; a prompt with no answer left fails the test.

    ``seen`` holds, for every prompt, everything printed before it. ``out`` and ``err`` are
    stdout and stderr and ``screen`` is both; ``log`` is every message the ``cnsr`` logger got
    while the terminal was open, one per line; ``text`` is the screen and the log together.
    ``close`` detaches the log handler (the ``fake_deps`` fixture does it after each test).
    """

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.prompts: list[str] = []
        self.seen: list[str] = []
        self.out = io.StringIO()
        self.err = io.StringIO()
        self._log = io.StringIO()
        self._handler = logging.StreamHandler(self._log)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(LOGGER_NAME)
        self._level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(self._handler)

    def close(self) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        logger.removeHandler(self._handler)
        logger.setLevel(self._level)

    def clear(self) -> None:
        """Forget everything shown and logged so far, and the prompts."""
        for stream in (self.out, self.err, self._log):
            stream.seek(0)
            stream.truncate()
        self.prompts.clear()
        self.seen.clear()

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.seen.append(self.out.getvalue())
        if not self.inputs:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.inputs.pop(0)

    def ui(self, interactive: bool = True, display: StageDisplay | None = None) -> Ui:
        return Ui(
            input_fn=self.read, secret_fn=self.read, out=self.out, err=self.err, interactive=interactive, display=display
        )

    @property
    def screen(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    @property
    def log(self) -> str:
        return self._log.getvalue()

    @property
    def text(self) -> str:
        return self.screen + self.log
