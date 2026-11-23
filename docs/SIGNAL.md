# Radio signal check (tools/zbx_signal.py, MDP-SIGNAL)

Signal (RSSI) and SNR of every site radio at a busy hour, from readings Zabbix already has (no new polling).

- Daily: average of SIGNAL_TIME (15:00) to +SIGNAL_WINDOW (10) minutes, compared with the day before.
- Weekly: average of the week's daily snapshots, compared with the week before; lowest day shown.
- Columns: device, IP, linked to, signal and SNR (before / now / change), far-end signal / SNR (Wave),
  capacity (Wave) or MCS (Cambium) or stations (AP), link drops in the period, status OK / WATCH / BAD / NO DATA.
- Endpoint devices (SIGNAL_EXCLUDE ^EP-) left out. LACP radios (SIGNAL_LACP) named in a note, not in the table.
- Thresholds: SIGNAL_RSSI_WATCH -65, SIGNAL_RSSI_BAD -70, SIGNAL_SNR_WATCH 20, SIGNAL_SNR_BAD 15, SIGNAL_DROP 5,
  SIGNAL_SAME 2, SIGNAL_DROPS_WATCH 3 (all in ~/.zabbix_env or /etc/mdatapulse/mdatapulse.conf).
- Files: REPORT_DIR/signal-daily, signal-weekly, signal-snap (cached snapshots).
- Web: /signal (list, date picker), /signal/now/daily, /signal/now/weekly, /signal/build. Home card + top menu.
- Schedule: hourly cron (`zbx_signal.py cron --apply`, /etc/cron.d/mdp-signal-report); builds once 15:00 passed.

Test: python3 tests/test_signal.py
