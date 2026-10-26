# -*- coding: utf-8 -*-
"""Tests für Steuerungs-Daemon Start/Stop."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from control_algorithm import daemon_control as dc


@patch.object(dc, "start_daemon", return_value=True)
@patch.object(dc, "stop_daemon")
@patch.object(dc, "is_daemon_running", return_value=True)
def test_ensure_control_daemon_keeps_running_instance(mock_running, mock_stop, mock_start):
    """Speichern darf laufenden Terminal-Daemon nicht beenden."""
    assert dc.ensure_control_daemon(restart=False) is True
    mock_stop.assert_not_called()
    mock_start.assert_not_called()


@patch.object(dc, "start_daemon", return_value=True)
@patch.object(dc, "stop_daemon")
@patch.object(dc, "is_daemon_running", side_effect=[False, False])
def test_ensure_control_daemon_starts_when_missing(mock_running, mock_stop, mock_start):
    assert dc.ensure_control_daemon(restart=False) is True
    mock_stop.assert_not_called()
    mock_start.assert_called_once()


@patch.object(dc, "start_daemon", return_value=True)
@patch.object(dc, "stop_daemon")
@patch.object(dc, "is_daemon_running", side_effect=[True, False, True])
def test_ensure_control_daemon_restart_stops_then_starts(mock_running, mock_stop, mock_start):
    assert dc.ensure_control_daemon(restart=True) is True
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
