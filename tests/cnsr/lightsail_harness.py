"""A real boto3 Lightsail client with dummy credentials, answered by botocore's Stubber."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from cnsr.aws.lightsail import Lightsail

REGION = "ap-northeast-1"


class Aws:
    """A real boto3 Lightsail client with dummy credentials, answered by a Stubber."""

    def __init__(self) -> None:
        self.client = boto3.client(
            "lightsail", region_name=REGION, aws_access_key_id="dummy", aws_secret_access_key="dummy"
        )
        self.stubber = Stubber(self.client)
        self.sleeps: list[float] = []
        # The clock waits measure on: sleeps move it on, and each AWS call by ``call_seconds``.
        self.now, self.call_seconds = 0.0, 0.0
        self.client.meta.events.register("before-parameter-build.lightsail", self._call_takes_time)
        self.ls = Lightsail("jp-tokyo", client=self.client, sleep=self._sleep, clock=lambda: self.now)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def _call_takes_time(self, **_) -> None:
        self.now += self.call_seconds

    def ok(self, operation: str, response: dict | None = None, **params) -> None:
        self.stubber.add_response(operation, response or {}, params or None)

    def fail(self, operation: str, code: str, message: str = "details", **params) -> None:
        self.stubber.add_client_error(operation, code, message, 400, expected_params=params or None)

    def pairs(self, *pairs: tuple[str, str], token: str | None = None, **params) -> None:
        page = {"keyPairs": [{"name": n, "fingerprint": f} for n, f in pairs]}
        if token:
            page["nextPageToken"] = token
        self.ok("get_key_pairs", page, includeDefaultKeyPair=False, **params)

    def instance(self, **fields) -> None:
        self.ok("get_instance", {"instance": {"name": "jp-1", **fields}}, instanceName="jp-1")

    def static_ip(self, address: str = "192.0.2.10", **fields) -> None:
        self.ok("get_static_ip", {"staticIp": {"name": "jp-1-ip", "ipAddress": address, **fields}}, staticIpName="jp-1-ip")
