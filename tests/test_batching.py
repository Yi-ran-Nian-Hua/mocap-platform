import queue
import unittest
import tempfile
import threading
import multiprocessing as mp
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
from contextlib import nullcontext

from server import EventBatcher, CaptureStore, Application
import server


def emit_spawned_batches(events, dropped):
    batcher = EventBatcher(events, dropped)
    for frame in range(4096):
        batcher.emit({'type':'tracker','id':300,'timestamp':100+frame/120,
                      'frame':frame,'pos':[1,2,3],'quat':[0,0,0,1]})
    batcher.flush()


class BatchingTests(unittest.TestCase):
    def test_spawn_queue_drains_and_records_every_event(self):
        with tempfile.TemporaryDirectory() as directory:
            context = mp.get_context('spawn')
            events, dropped = context.Queue(maxsize=64), context.Value('L', 0)
            process = context.Process(target=emit_spawned_batches, args=(events,dropped))
            app = Application(Path(directory))
            name = app.store.start_recording({})
            process.start()
            try:
                app._consume(process, events, app.generation)
                process.join(3)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(dropped.value, 0)
                import json
                rows = [json.loads(line) for line in app.store.resolve_recording(name).read_text().splitlines()][1:]
                self.assertEqual([row['frame'] for row in rows], list(range(4096)))
                self.assertEqual(rows[-1]['timestamp'], 100+4095/120)
            finally:
                if process.is_alive(): process.terminate(); process.join()
                events.close()

    def test_snapshot_copy_does_not_block_ingestion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(Path(directory))
            store.ingest({'type':'tracker','id':42,'pos':[1,2,3]})
            copying, release, ingested = threading.Event(), threading.Event(), threading.Event()
            original = server.copy.deepcopy
            def slow_copy(value):
                if isinstance(value, dict) and value.get('id') == 42:
                    copying.set()
                    release.wait(2)
                return original(value)
            with patch('server.copy.deepcopy', side_effect=slow_copy):
                snapshot = threading.Thread(target=store.snapshot)
                snapshot.start()
                self.assertTrue(copying.wait(1))
                def ingest():
                    store.ingest({'type':'tracker','id':43,'pos':[4,5,6]})
                    ingested.set()
                receiver = threading.Thread(target=ingest)
                receiver.start()
                progressed = ingested.wait(.5)
                release.set()
                snapshot.join()
                receiver.join()
                self.assertTrue(progressed, 'snapshot held the receiver lock while copying')

    def test_consumer_records_entire_batch_with_original_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Application(Path(directory))
            name = app.store.start_recording({})
            handle = Mock(wraps=app.store.file)
            app.store.file = handle
            events = queue.Queue()
            events.put([{'type':'tracker','id':6,'timestamp':123,'pos':[1,2,3]},
                        {'type':'tracker','id':300,'timestamp':124,'pos':[4,5,6]}])
            class Finished:
                exitcode = 0
                def is_alive(self): return False
            app._consume(Finished(), events, app.generation)
            import json
            rows = [json.loads(line) for line in app.store.resolve_recording(name).read_text().splitlines()][1:]
            self.assertEqual([row['timestamp'] for row in rows], [123,124])
            self.assertEqual([row['type'] for row in rows], ['rigid_body','skeleton_joint'])
            self.assertEqual(handle.flush.call_count, 1)

    def test_preserves_all_events_in_order_and_flushes_partial_batch(self):
        events = queue.Queue()
        dropped = SimpleNamespace(value=0, get_lock=nullcontext)
        batcher = EventBatcher(events, dropped, batch_size=3)
        source = [{'type':'tracker', 'id':i, 'timestamp':100+i} for i in range(8)]
        for event in source: batcher.emit(event)
        self.assertEqual(events.qsize(), 2)
        batcher.flush()
        received = []
        while not events.empty(): received.extend(events.get_nowait())
        self.assertEqual(received, source)
        self.assertEqual(dropped.value, 0)
        batcher.flush()
        self.assertTrue(events.empty())

    def test_full_queue_counts_events_not_batches_and_recovers(self):
        events = queue.Queue(maxsize=1)
        dropped = SimpleNamespace(value=0, get_lock=nullcontext)
        batcher = EventBatcher(events, dropped, batch_size=3)
        for i in range(6): batcher.emit({'id':i})
        self.assertEqual(dropped.value, 3)
        self.assertEqual([x['id'] for x in events.get()], [0,1,2])
        batcher.emit({'id':6})
        batcher.flush()
        self.assertEqual(events.get(), [{'id':6}])


if __name__ == '__main__': unittest.main()
