"""Live preview commands, imported without loading the optional audio backend."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import soundfile as sf
import typer

from .realtime import BLOCK, RATE, Capture, Detector, Timeline, speakers


def devices():
    """List Windows playback devices (IDs accepted by listen --device)."""
    try:
        typer.echo(json.dumps(speakers(), ensure_ascii=False, indent=2))
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def listen(
    device: Annotated[str | None, typer.Option(help="Playback device ID or name.")] = None,
    input: Annotated[
        Path | None, typer.Option(help="Replay a file instead of system audio.")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Save live.v1 observations as JSONL.")] = None,
    duration: Annotated[
        float | None, typer.Option(min=0.1, help="Stop after audio seconds.")
    ] = None,
    fast: Annotated[bool, typer.Option(help="File replay without pacing (testing).")] = False,
):
    """Preview live chords. Ctrl+C stops; advanced harmony is deferred to offline analysis."""
    if device and input:
        raise typer.BadParameter("--device and --input are mutually exclusive")
    if fast and not input:
        raise typer.BadParameter("--fast requires --input")
    if output and input and output.resolve() == input.resolve():
        raise typer.BadParameter("output must differ from input")
    try:
        with ExitStack() as stack:
            # Exclusive creation prevents destroying an earlier session.
            sink = stack.enter_context(output.open("x", encoding="utf-8")) if output else None
            if input:
                audio = stack.enter_context(sf.SoundFile(input))
                rate = audio.samplerate
                block = max(1, round(rate * BLOCK / RATE))

                def replay():
                    start, began = 0, time.monotonic()
                    while len(data := audio.read(block, dtype="float32", always_2d=True)):
                        if not fast:
                            time.sleep(
                                max(0, began + (start + len(data)) / rate - time.monotonic())
                            )
                        yield start, data
                        start += len(data)

                source = replay()
            else:
                capture = stack.enter_context(Capture(device))
                rate, source = RATE, capture.blocks()
            detector, history = Detector(rate=rate), Timeline()
            typer.echo("Live DSP preview; no key/function conclusions. Ctrl+C to stop.", err=True)
            try:
                for start, data in source:
                    if duration is not None:
                        data = data[: max(0, round(duration * rate) - start)]
                        if not len(data):
                            break
                    event = detector.feed(data, start)
                    if event:
                        history.append(event)
                        line = json.dumps(event.to_dict(), ensure_ascii=False)
                        typer.echo(line)
                        if sink:
                            sink.write(line + "\n")
                            sink.flush()
                    if duration is not None and detector.end >= round(duration * rate):
                        break
            except KeyboardInterrupt:
                pass
            typer.echo("Timeline: " + " → ".join(e.chord for e in history.items), err=True)
            if not input:
                typer.echo(f"Dropped capture blocks: {capture.dropped}", err=True)
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def web(
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8765,
):
    """Open the local live-chord dashboard at http://127.0.0.1:8765."""
    from .web import serve

    try:
        serve(port)
    except OSError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
