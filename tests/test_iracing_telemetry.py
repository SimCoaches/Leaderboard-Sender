"""iRacing return telemetry uses SDK frames, not GUI reads of a mutable buffer."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


FIXTURE = r'''
from unittest.mock import Mock, patch
import lap_time_sender as s

class FakeSDK:
    def __init__(self):
        self.is_initialized = self.is_connected = True
        self.shutdown = Mock()
        self.startup = Mock(return_value=True)
        self.live = dict(SessionTime=100.0, Speed=25.0, Brake=0.0,
                         IsOnTrack=True, IsReplayPlaying=False,
                         PlayerCarInPitStall=False, PlayerCarTowTime=0.0, SessionNum=1,
                         Gear=3, LapCurrentLapTime=12.0, LapLastLapTime=None, Lap=1)
        self.frozen = {}
        self.freeze_calls = 0
    def freeze_var_buffer_latest(self):
        self.freeze_calls += 1
        self.frozen = dict(self.live)
    def __getitem__(self, key):
        return self.frozen[key]

sdk = FakeSDK()
with patch.object(s.irsdk, 'IRSDK', return_value=sdk):
    monitor = s.iRacingMonitor()
monitor.state.ir_connected = True
clock = patch.object(s.time, 'monotonic', return_value=10.0)
now = clock.start()
'''


class IRacingTelemetryTests(unittest.TestCase):
    def run_scenario(self, scenario):
        script = FIXTURE + '\n' + textwrap.dedent(scenario)
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-'], input=script, encoding='utf-8',
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_freezes_sdk_before_capture_and_returns_independent_snapshots(self):
        self.run_scenario(r'''
            assert monitor.return_to_pits_snapshot() is None
            monitor.display_telemetry()
            assert sdk.freeze_calls == 1
            snapshot = monitor.return_to_pits_snapshot()
            assert snapshot == dict(connected=True, in_car=True, speed_mps=25.0, brake=0.0,
                                    sampled_at=10.0, in_pit_stall=False, tow_time=0.0, session_num=1,
                                    telemetry_generation=0)
            snapshot['speed_mps'] = 999
            sdk.live['Speed'] = 999
            assert monitor.return_to_pits_snapshot()['speed_mps'] == 25.0
            assert monitor.return_to_pits_snapshot() is not monitor.return_to_pits_snapshot()
        ''')

    def test_repeated_frame_does_not_refresh_age_or_other_fields(self):
        self.run_scenario(r'''
            monitor.display_telemetry()
            original = monitor.return_to_pits_snapshot()
            now.return_value = 20.0
            sdk.live.update(Speed=0.0, Brake=1.0, PlayerCarInPitStall=True)
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() == original
            sdk.live['SessionTime'] += 0.1
            monitor.display_telemetry()
            updated = monitor.return_to_pits_snapshot()
            assert updated['sampled_at'] == 20.0
            assert updated['speed_mps'] == 0.0 and updated['brake'] == 1.0
            assert updated['in_pit_stall'] is True
        ''')

    def test_missing_and_nonfinite_session_time_invalidate_previous_sample(self):
        self.run_scenario(r'''
            for invalid in (None, float('nan'), float('inf'), '100.0', True, False):
                sdk.live['SessionTime'] = 100.0
                monitor.display_telemetry()
                assert monitor.return_to_pits_snapshot() is not None
                sdk.live['SessionTime'] = invalid
                monitor.display_telemetry()
                assert monitor.return_to_pits_snapshot() is None, invalid
            sdk.live['SessionTime'] = 100.0
            monitor.display_telemetry()
            del sdk.live['SessionTime']
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() is None
        ''')

    def test_session_time_rollback_invalidates_then_marks_new_generation(self):
        self.run_scenario(r'''
            monitor.display_telemetry()
            previous = monitor.return_to_pits_snapshot()
            sdk.live['SessionTime'] = 1.0
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() is None
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() is None
            sdk.live['SessionTime'] += 0.1
            now.return_value = 11.0
            monitor.display_telemetry()
            current = monitor.return_to_pits_snapshot()
            assert current['sampled_at'] == 11.0
            assert current['telemetry_generation'] > previous['telemetry_generation']
        ''')

    def test_replay_and_not_in_car_are_never_marked_ready(self):
        self.run_scenario(r'''
            for on_track, replay, expected in ((True, False, True), (True, True, None),
                                                (False, False, False), (False, True, False),
                                                (True, None, None), (None, False, None)):
                sdk.live.update(IsOnTrack=on_track, IsReplayPlaying=replay)
                sdk.live['SessionTime'] += 0.1
                monitor.display_telemetry()
                assert monitor.return_to_pits_snapshot()['in_car'] is expected
        ''')

    def test_missing_flags_stay_unknown_and_cannot_be_mistaken_for_confirmed_exit(self):
        self.run_scenario(r'''
            for missing in ('IsOnTrack', 'IsReplayPlaying', 'PlayerCarInPitStall'):
                sdk.live.update(IsOnTrack=True, IsReplayPlaying=False, PlayerCarInPitStall=False)
                del sdk.live[missing]
                sdk.live['SessionTime'] += 0.1
                monitor.display_telemetry()
                snapshot = monitor.return_to_pits_snapshot()
                field = 'in_pit_stall' if missing == 'PlayerCarInPitStall' else 'in_car'
                assert snapshot[field] is None, (missing, snapshot)
        ''')

    def test_return_snapshot_never_reads_or_requires_clutch_telemetry(self):
        self.run_scenario(r'''
            assert 'Clutch' not in sdk.live
            monitor.display_telemetry()
            snapshot = monitor.return_to_pits_snapshot()
            assert snapshot is not None and snapshot['in_car'] is True
            assert 'clutch_disengaged' not in snapshot
            sdk.live['Clutch'] = object()
            reads = []
            original_getitem = FakeSDK.__getitem__
            def traced_read(instance, key):
                reads.append(key)
                return original_getitem(instance, key)
            with patch.object(FakeSDK, '__getitem__', traced_read):
                sdk.live['SessionTime'] += 0.1
                monitor.display_telemetry()
            assert 'Clutch' not in reads
            snapshot = monitor.return_to_pits_snapshot()
            assert snapshot is not None and snapshot['in_car'] is True
            assert 'clutch_disengaged' not in snapshot
        ''')

    def test_disconnect_and_monitor_stop_clear_snapshot(self):
        self.run_scenario(r'''
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() is not None
            sdk.is_connected = False
            monitor.check_iracing()
            assert monitor.return_to_pits_snapshot() is None
            assert not monitor.state.ir_connected
            sdk.shutdown.assert_called_once()
            sdk.is_connected = True
            monitor.check_iracing()
            assert monitor.state.ir_connected
            assert monitor.return_to_pits_snapshot() is None
            monitor.display_telemetry()
            assert monitor.return_to_pits_snapshot() is not None
            monitor.stop()
            assert not monitor.running
            assert monitor.return_to_pits_snapshot() is None
            assert not monitor.state.ir_connected
        ''')


if __name__ == '__main__':
    unittest.main()
