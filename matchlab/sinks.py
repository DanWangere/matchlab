"""Where a replayed event stream goes: stdout, a file, an HTTP endpoint, or an Event Hub
compatible endpoint (what a Fabric Eventstream custom-endpoint source exposes)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Iterable


class StdoutSink:
    def send(self, batch: list[dict[str, Any]]) -> None:
        for d in batch:
            sys.stdout.write(json.dumps(d, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        pass


class FileSink:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")

    def send(self, batch: list[dict[str, Any]]) -> None:
        for d in batch:
            self.f.write(json.dumps(d, separators=(",", ":")) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


class HttpSink:
    """POSTs each batch as a JSON array. Useful for a local renderer or an Azure Function."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def send(self, batch: list[dict[str, Any]]) -> None:
        req = urllib.request.Request(self.url, data=json.dumps(batch).encode("utf-8"), headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    def close(self) -> None:
        pass


class EventHubSink:
    """Sends to an Event Hub compatible endpoint. Set EVENTHUB_CONNECTION_STRING and EVENTHUB_NAME
    from the Eventstream custom-endpoint source's 'Event Hub' keys. Requires `pip install azure-eventhub`."""

    def __init__(self, connection_string: str | None = None, name: str | None = None):
        from azure.eventhub import EventData, EventHubProducerClient  # type: ignore

        cs = connection_string or os.environ["EVENTHUB_CONNECTION_STRING"]
        nm = name or os.environ["EVENTHUB_NAME"]
        self._EventData = EventData
        self.client = EventHubProducerClient.from_connection_string(cs, eventhub_name=nm)

    def send(self, batch: list[dict[str, Any]]) -> None:
        edb = self.client.create_batch()
        for d in batch:
            edb.add(self._EventData(json.dumps(d, separators=(",", ":"))))
        self.client.send_batch(edb)

    def close(self) -> None:
        self.client.close()


def make_sink(spec: str) -> Any:
    if spec == "stdout":
        return StdoutSink()
    if spec.startswith("file:"):
        return FileSink(spec[len("file:"):])
    if spec.startswith("http://") or spec.startswith("https://"):
        return HttpSink(spec)
    if spec == "eventhub":
        return EventHubSink()
    raise ValueError(f"unknown sink {spec!r}; use stdout, file:<path>, http(s)://..., or eventhub")


def read_jsonl(path: str) -> Iterable[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
