import asyncio
from collections import Callable
from dataclasses import dataclass
from typing import Optional

from device.devicetypes.channel import ReadWriteChannel, ReadableChannel, \
    WriteableChannel
from device.inmemory.channel import InMemoryReadWriteChannel

_DEFAULT_CLOCK_SECONDS = 0.1


@dataclass
class Trigger:
    input: ReadWriteChannel[Optional[ReadableChannel[float]]]
    output: ReadWriteChannel[Optional[WriteableChannel[bool]]]
    min_value: ReadWriteChannel[float]
    max_value: ReadWriteChannel[float]
    delay_seconds: ReadWriteChannel[float]

    async def run_in_memory(self):
        input_channel = (await self.input.get()).value
        output_channel = (await self.output.get()).value
        current = (await input_channel.get()).value
        min_value = (await self.min_value.get()).value
        max_value = (await self.max_value.get()).value
        delay = (await self.delay_seconds.get()).value
        if min_value <= current <= max_value:
            await asyncio.sleep(delay)
            await output_channel.put(True)


def in_memory_trigger() -> Trigger:
    return Trigger(
        input=InMemoryReadWriteChannel(None),
        min_value=InMemoryReadWriteChannel(0.0),
        max_value=InMemoryReadWriteChannel(0.0),
        output=InMemoryReadWriteChannel(None),
        delay_seconds=InMemoryReadWriteChannel(0.0)
    )


@dataclass
class FakeTriggerBox:
    trigger_1: Trigger
    trigger_2: Trigger

    min_seconds_between_checks: ReadWriteChannel[float]

    async def run_in_memory(self):
        while True:
            await asyncio.gather([
                self.trigger_1.run_in_memory,
                self.trigger_2.run_in_memory
            ])
            delay = await self.min_seconds_between_checks.get()
            await asyncio.sleep(delay.value)

    def run_forever(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.run_in_memory())


def in_memory_box() -> FakeTriggerBox:
    return FakeTriggerBox(
        trigger_1=in_memory_trigger(),
        trigger_2=in_memory_trigger(),
        min_seconds_between_checks=InMemoryReadWriteChannel(
            _DEFAULT_CLOCK_SECONDS)
    )


def in_memory_box_running() -> FakeTriggerBox:
    box = in_memory_box()
    box.run_in_memory()
    return box