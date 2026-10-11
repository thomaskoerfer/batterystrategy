"""Tests for incomplete feature-slot persistence."""

from custom_components.battery_strategy.feature_checkpoint import (
    CHECKPOINT_SAVE_DELAY_S,
    FeatureAggregatorCheckpointStore,
)


def test_repeated_schedule_does_not_postpone_bounded_checkpoint():
    callbacks = []

    class FakeStore:
        @staticmethod
        def async_delay_save(callback, delay):
            callbacks.append((callback, delay))

    checkpoint_store = object.__new__(FeatureAggregatorCheckpointStore)
    checkpoint_store._store = FakeStore()
    checkpoint_store._save_scheduled = False
    current = {"sequence": 1}

    checkpoint_store.schedule(lambda: dict(current))
    current["sequence"] = 2
    checkpoint_store.schedule(lambda: dict(current))

    assert len(callbacks) == 1
    callback, delay = callbacks.pop()
    assert delay == CHECKPOINT_SAVE_DELAY_S
    assert callback() == {"sequence": 2}

    checkpoint_store.schedule(lambda: dict(current))
    assert len(callbacks) == 1
