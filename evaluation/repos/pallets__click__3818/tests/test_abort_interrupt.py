"""A second Ctrl-C while ``Command.main()`` reported an abort or an error used
to escape as an unhandled traceback: the message and the exit both ran in
``except`` clauses, outside every ``try`` that caught ``KeyboardInterrupt``.
https://github.com/pallets/click/issues/3802

The fix holds one policy, the one the pager has followed since
https://github.com/pallets/click/pull/351: a late interrupt must not replace
the intended outcome. In standalone mode the collected exit code wins, for
aborts, errors and success alike, and the message is best effort. Without
standalone mode the interrupt propagates to the caller. Two settled contracts
stay in place: the first interrupt reaches a non-standalone caller as
``Abort`` (https://github.com/pallets/click/pull/2380), and the ``Aborted!``
message stays as it is (declined in
https://github.com/pallets/click/issues/1447 and
https://github.com/pallets/click/issues/2584).
"""

import os
import select
import signal
import subprocess
import sys
import time

import pytest

import click
from click._compat import WIN

if not WIN:
    import pty


class RecordingStderr:
    """A stderr stand-in that records what is written to it.

    With ``interrupt_at=N``, the ``N``-th ``isatty()`` call raises
    ``KeyboardInterrupt``. ``echo()`` calls ``isatty()`` before writing, which
    is where the traceback in issue #3802 ended.
    """

    def __init__(self, interrupt_at=None):
        self.text = ""
        self.interrupt_at = interrupt_at
        self.isatty_calls = 0

    def isatty(self):
        self.isatty_calls += 1

        if self.isatty_calls == self.interrupt_at:
            raise KeyboardInterrupt

        return False

    def write(self, value):
        self.text += value
        return len(value)

    def flush(self):
        pass


def _interrupt_prompt(text):
    """Stand in for ``input()`` when the user presses Ctrl-C at a prompt."""
    raise KeyboardInterrupt


def _prompting_command():
    @click.command()
    @click.option("--name", prompt="Name")
    def cli(name):
        click.echo(name)

    return cli


def _interrupting_command():
    @click.command()
    def cli():
        raise KeyboardInterrupt

    return cli


def _exit_code(cli, stderr, monkeypatch):
    """Run *cli* in standalone mode and return its exit code."""
    monkeypatch.setattr(sys, "stderr", stderr)

    try:
        cli.main([], "cli")
    except SystemExit as e:
        return e.code
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped Command.main()")

    pytest.fail("Command.main() returned without exiting")


@pytest.mark.parametrize(
    ("source", "interrupt_at"),
    [("prompt", None), ("prompt", 1), ("body", None), ("body", 1), ("body", 2)],
)
def test_interrupt_while_reporting_abort(monkeypatch, source, interrupt_at):
    """Exit code 1 survives a second Ctrl-C; the message may not.

    The counted ``isatty`` picks the window. A prompt converts the interrupt
    to ``Abort`` itself, so the only window left in ``Command.main()`` is the
    ``Aborted!`` message (``1``). An interrupt from the command body goes
    through the conversion in ``Command.main()``, which writes a blank line
    first (``1``), before the message (``2``).
    """
    if source == "prompt":
        monkeypatch.setattr("click.termui.visible_prompt_func", _interrupt_prompt)
        cli = _prompting_command()
    else:
        cli = _interrupting_command()

    stderr = RecordingStderr(interrupt_at=interrupt_at)

    assert _exit_code(cli, stderr, monkeypatch) == 1

    if interrupt_at is None:
        assert "Aborted!" in stderr.text


def test_interrupt_while_reporting_error(monkeypatch):
    """A Ctrl-C while Click writes an error keeps the error's exit code."""

    class InterruptedReport(click.ClickException):
        exit_code = 3

        def show(self, file=None):
            raise KeyboardInterrupt

    @click.command()
    def cli():
        raise InterruptedReport("boom")

    assert _exit_code(cli, RecordingStderr(), monkeypatch) == 3


def test_late_interrupt_with_disabled_standalone_mode(monkeypatch):
    """Without standalone mode, a late interrupt reaches the caller, not exit 1.

    The patched ``Abort`` raises a second ``KeyboardInterrupt`` from its
    constructor: it lands on the ``raise Abort() from e`` statement that
    ``Command.main()`` only runs for non-standalone callers.
    """

    class InterruptedAbort(click.Abort):
        def __init__(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(click.core, "Abort", InterruptedAbort)
    cli = _interrupting_command()
    monkeypatch.setattr(sys, "stderr", RecordingStderr())

    with pytest.raises(KeyboardInterrupt):
        cli.main([], "cli", standalone_mode=False)


def test_interrupt_while_exiting_after_success(monkeypatch):
    """A Ctrl-C while a successful exit propagates keeps exit code 0.

    The uniform policy: the run completed before the interrupt arrived, so the
    intended outcome wins for success exactly as it does for aborts and
    errors.
    """

    @click.command()
    def cli():
        click.echo("done")

    real_exit = sys.exit
    codes = []

    def interrupted_exit(code=None):
        codes.append(code)

        if len(codes) == 1:
            raise KeyboardInterrupt

        real_exit(code)

    monkeypatch.setattr(sys, "exit", interrupted_exit)

    assert _exit_code(cli, RecordingStderr(), monkeypatch) == 0
    assert codes == [0, 0]


# Same scenario, driven with real signals through a real pty.

CHILD_SCRIPT = """\
import click


@click.command()
@click.option("--name", prompt="Name")
def cli(name):
    click.echo(name)


cli()
"""


def _drain(fd, timeout):
    """Read whatever the pty holds, for up to *timeout* seconds."""
    out = b""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)

        if not ready:
            continue

        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break

        if not chunk:
            break

        out += chunk

    return out


def _busy_wait(seconds):
    """Wait with microsecond resolution, which ``time.sleep`` lacks."""
    end = time.perf_counter() + seconds

    while time.perf_counter() < end:
        pass


def _run_interrupted(script, delay):
    """Run *script* under a pty and send two ``SIGINT``, *delay* apart."""
    primary, replica = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=replica,
        stdout=replica,
        stderr=replica,
        start_new_session=True,
        close_fds=True,
    )
    os.close(replica)

    try:
        output = b""
        deadline = time.monotonic() + 10

        while b"Name" not in output and time.monotonic() < deadline:
            output += _drain(primary, 0.2)

        pgid = os.getpgid(proc.pid)

        try:
            os.killpg(pgid, signal.SIGINT)
            _busy_wait(delay)

            if proc.poll() is None:
                os.killpg(pgid, signal.SIGINT)
        except OSError:
            pass

        # Keep reading while the child exits, or the pty buffer is lost.
        deadline = time.monotonic() + 5

        while proc.poll() is None and time.monotonic() < deadline:
            output += _drain(primary, 0.1)

        output += _drain(primary, 0.3)
    finally:
        if proc.poll() is None:
            proc.kill()

        proc.wait()
        os.close(primary)

    return output.decode(errors="replace")


# A traceback frame from Click mentions this path.
CLICK_CORE = os.path.join("click", "core.py")


@pytest.mark.stress
@pytest.mark.skipif(WIN, reason="needs a pty and POSIX signals")
@pytest.mark.parametrize("delay_us", range(0, 800, 20))
def test_stress_double_interrupt_at_prompt(tmp_path, delay_us):
    """Two real SIGINT at a prompt must not produce a Click traceback.

    ``Aborted!`` is not asserted: the second interrupt can land before Click
    writes it.
    """
    script = tmp_path / "prompting_app.py"
    script.write_text(CHILD_SCRIPT, encoding="UTF-8")

    output = _run_interrupted(script, delay_us / 1_000_000)

    assert CLICK_CORE not in output, output
